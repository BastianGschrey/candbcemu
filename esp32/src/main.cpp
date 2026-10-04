// CAN demo sender for ESP32 + MCP2515: loads a DBC (uploaded in the browser, kept in flash) and
// sends demo data for every message. Web UI on port 80; Wi-Fi from the UI (access point
// "CAN-Sender-ESP" until a network is configured). Wiring: CS=5 SCK=18 MISO=19 MOSI=23.
#include <Arduino.h>
#include <ArduinoJson.h>
#include <ArduinoOTA.h>
#include <ESPmDNS.h>
#include <LittleFS.h>
#include <Preferences.h>
#include <SPI.h>
#include <WebServer.h>
#include <WiFi.h>
#include <mcp2515.h>

#include "dbc.h"

static const int PIN_CS = 5, PIN_SCK = 18, PIN_MISO = 19, PIN_MOSI = 23;
static const char *HOSTNAME = "can-esp";
static const char *AP_NAME = "CAN-Sender-ESP";
static const char *AP_PASS = "cansender";
static const size_t MAX_DBC_BYTES = 96 * 1024;

extern const uint8_t index_html_start[] asm("_binary_src_index_html_start");
extern const uint8_t index_html_end[] asm("_binary_src_index_html_end");

MCP2515 mcp(PIN_CS);
WebServer server(80);
Preferences prefs;

static dbc::Database db;
static String dbcName;
static bool running = false;
static int bitrate = 500000;
static int clockMHz = 8;
static unsigned long startedAt = 0;
static String logLine = "";
static uint8_t tec = 0, rec = 0, eflg = 0;
static unsigned long lastStatus = 0;
static File uploadFile;

// The web server runs in its own task (core 0), CAN transmission in loop() (core 1): a slow HTTP
// request must never delay a frame. Everything that touches `db`, the MCP2515 or the run state is
// done under this mutex; network I/O is not.
static SemaphoreHandle_t g_lock;
struct Lock {
    Lock() { xSemaphoreTakeRecursive(g_lock, portMAX_DELAY); }
    ~Lock() { xSemaphoreGiveRecursive(g_lock); }
};
static String uploadName, uploadError;

static void note(const String &s) {
    logLine = s;
    Serial.println(s);
}

// ---------------------------------------------------------------- DBC files
static String safeName(String n) {
    int slash = n.lastIndexOf('/');
    if (slash >= 0) n = n.substring(slash + 1);
    String out;
    for (char c : n)
        if (isalnum((unsigned char)c) || c == '_' || c == '-' || c == '.') out += c;
    if (!out.endsWith(".dbc")) out += ".dbc";
    if (out.length() > 40) out = out.substring(out.length() - 40);
    return out;
}

static bool loadDbc(const String &name) {
    File f = LittleFS.open("/dbc/" + name, "r");
    if (!f) return false;
    if (f.size() > MAX_DBC_BYTES) { f.close(); return false; }
    std::string text;
    text.reserve(f.size());
    while (f.available()) text += (char)f.read();
    f.close();
    dbc::Database fresh;
    std::string err;
    if (!dbc::parse(text, fresh, err)) {
        note("DBC " + name + ": " + String(err.c_str()));
        return false;
    }
    {
        Lock lock;
        db = std::move(fresh);
        dbcName = name;
        prefs.putString("dbc", name);
        unsigned long now = millis();
        for (auto &m : db.messages) m.nextDue = now;
    }
    note("DBC " + name + ": " + String((unsigned)db.messages.size()) + " messages");
    return true;
}

static std::vector<String> listDbcs() {
    std::vector<String> names;
    File dir = LittleFS.open("/dbc");
    if (!dir) return names;
    for (File f = dir.openNextFile(); f; f = dir.openNextFile()) {
        String n = f.name();
        if (n.endsWith(".dbc")) names.push_back(n);
    }
    std::sort(names.begin(), names.end());
    return names;
}

// ---------------------------------------------------------------- CAN
static CAN_SPEED speedFor(int bps) {
    switch (bps) {
        case 125000: return CAN_125KBPS;
        case 250000: return CAN_250KBPS;
        case 1000000: return CAN_1000KBPS;
        default: return CAN_500KBPS;
    }
}

static bool canStart(int bps, int mhz) {
    Lock lock;
    mcp.reset();
    CAN_CLOCK clk = mhz == 16 ? MCP_16MHZ : mhz == 20 ? MCP_20MHZ : MCP_8MHZ;
    if (mcp.setBitrate(speedFor(bps), clk) != MCP2515::ERROR_OK || mcp.setNormalMode() != MCP2515::ERROR_OK) {
        note("MCP2515 antwortet nicht - Verkabelung (CS/SCK/MISO/MOSI, 5 V) pruefen");
        return false;
    }
    bitrate = bps;
    clockMHz = mhz;
    prefs.putInt("bitrate", bps);
    prefs.putInt("clock", mhz);
    startedAt = millis();
    for (auto &m : db.messages) m.nextDue = startedAt;
    running = true;
    note("sendet mit " + String(bps / 1000) + " kbit/s (Quarz " + String(mhz) + " MHz)");
    return true;
}

static void canStop() {
    Lock lock;
    running = false;
    mcp.setConfigMode();
    note("gestoppt");
}

static void transmit() {
    Lock lock;
    if (!running) return;
    unsigned long now = millis();
    double t = (now - startedAt) / 1000.0;
    for (auto &m : db.messages) {
        if (!m.enabled || now < m.nextDue) continue;
        dbc::step(m, t);
        dbc::encode(m, m.lastData);
        struct can_frame f;
        f.can_id = m.id | (m.extended ? CAN_EFF_FLAG : 0);
        f.can_dlc = m.dlc;
        memcpy(f.data, m.lastData, 8);
        if (mcp.sendMessage(&f) == MCP2515::ERROR_OK) m.sent++;
        else m.errors++;
        m.nextDue += m.cycleMs;
        if ((long)(m.nextDue - now) < -500) m.nextDue = now;   // far behind: do not burst
    }
    // Nobody needs received frames; empty the receive buffers so the controller never reports overflow.
    struct can_frame rx;
    while (mcp.checkReceive() && mcp.readMessage(&rx) == MCP2515::ERROR_OK) {}
    if (now - lastStatus > 1000) {
        lastStatus = now;
        tec = mcp.errorCountTX();
        rec = mcp.errorCountRX();
        eflg = mcp.getErrorFlags();
    }
}

// ---------------------------------------------------------------- web API
static const char *kindName(dbc::Signal::Kind k) {
    switch (k) {
        case dbc::Signal::Sine: return "sine";
        case dbc::Signal::Triangle: return "triangle";
        case dbc::Signal::Square: return "square";
        case dbc::Signal::Const: return "const";
        case dbc::Signal::Choices: return "choices";
        default: return "manual";
    }
}

static void sendJson(const JsonDocument &doc, int code = 200) {
    String out;
    serializeJson(doc, out);
    server.sendHeader("Cache-Control", "no-store");
    server.send(code, "application/json", out);
}

static void sendError(const String &msg, int code = 400) {
    JsonDocument d;
    d["error"] = msg;
    sendJson(d, code);
}

static bool bodyJson(JsonDocument &doc) {
    if (!server.hasArg("plain")) return false;
    return !deserializeJson(doc, server.arg("plain"));
}

static void handleState() {
    std::vector<String> names = listDbcs();
    String out;
    {
    Lock lock;
    JsonDocument d;
    d["dbc"] = dbcName;
    d["running"] = running;
    d["interface"] = "mcp2515";
    d["channel"] = "CS" + String(PIN_CS);
    d["bitrate"] = bitrate;
    d["clock"] = clockMHz;
    d["log"][0] = logLine;
    d["tec"] = tec;
    d["rec"] = rec;
    d["eflg"] = eflg;
    d["wifi"] = WiFi.getMode() == WIFI_AP ? String("AP ") + WiFi.softAPIP().toString() : WiFi.localIP().toString();
    d["heap"] = ESP.getFreeHeap();
    JsonArray nameArr = d["dbcs"].to<JsonArray>();
    for (auto &n : names) nameArr.add(n);
    JsonArray msgs = d["messages"].to<JsonArray>();
    for (size_t i = 0; i < db.messages.size(); i++) {
        auto &m = db.messages[i];
        JsonObject o = msgs.add<JsonObject>();
        o["key"] = (int)i;
        char idbuf[16];
        snprintf(idbuf, sizeof idbuf, "0x%X", (unsigned)m.id);
        o["id"] = idbuf;
        o["name"] = m.name;
        o["cycle"] = m.cycleMs;
        o["enabled"] = m.enabled;
        o["sent"] = m.sent;
        o["errors"] = m.errors;
        String raw;
        for (int b = 0; b < m.dlc; b++) {
            char h[4];
            snprintf(h, sizeof h, "%02X", m.lastData[b]);
            raw += h;
            if (b < m.dlc - 1) raw += ' ';
        }
        o["raw"] = raw;
        JsonArray sigs = o["signals"].to<JsonArray>();
        for (size_t k = 0; k < m.signals.size(); k++) {
            auto &s = m.signals[k];
            JsonObject so = sigs.add<JsonObject>();
            so["name"] = s.name;
            so["unit"] = s.unit;
            so["lo"] = s.rangeLo;
            so["hi"] = s.rangeHi;
            so["kind"] = kindName(s.kind);
            so["value"] = s.value;
            so["manual"] = s.manual;
            so["period"] = s.period;
            so["selector"] = (int)k == m.muxSwitch;
        }
    }
    serializeJson(d, out);
    }
    server.sendHeader("Cache-Control", "no-store");
    server.send(200, "application/json", out);   // the slow part (network) happens outside the lock
}

static void ok() {
    JsonDocument d;
    d["ok"] = true;
    sendJson(d);
}

static void handleDbc() {
    JsonDocument b;
    if (!bodyJson(b)) return sendError("bad request");
    String name = safeName(b["name"] | "");
    if (!loadDbc(name)) return sendError("DBC konnte nicht geladen werden");
    ok();
}

static void handleDelete() {
    Lock lock;
    JsonDocument b;
    if (!bodyJson(b)) return sendError("bad request");
    String name = safeName(b["name"] | "");
    LittleFS.remove("/dbc/" + name);
    if (name == dbcName) { db.messages.clear(); dbcName = ""; prefs.putString("dbc", ""); }
    ok();
}

static void handleConnect() {
    JsonDocument b;
    if (!bodyJson(b)) return sendError("bad request");
    if (db.messages.empty()) return sendError("zuerst eine DBC laden");
    int bps = b["bitrate"] | 500000;
    int mhz = b["clock"] | clockMHz;
    if (!canStart(bps, mhz)) return sendError(logLine, 500);
    ok();
}

static void handleDisconnect() { canStop(); ok(); }

static void handleAll() {
    Lock lock;
    JsonDocument b;
    if (!bodyJson(b)) return sendError("bad request");
    bool en = b["enabled"] | false;
    for (auto &m : db.messages) m.enabled = en;
    ok();
}

static void handleMessage() {
    Lock lock;
    JsonDocument b;
    if (!bodyJson(b)) return sendError("bad request");
    size_t key = b["key"] | -1;
    if (key >= db.messages.size()) return sendError("unknown message", 404);
    auto &m = db.messages[key];
    if (!b["enabled"].isNull()) { m.enabled = b["enabled"]; m.nextDue = millis(); }
    if (!b["cycle"].isNull()) m.cycleMs = constrain((int)b["cycle"], 5, 3600000);
    ok();
}

static void handleSignal() {
    Lock lock;
    JsonDocument b;
    if (!bodyJson(b)) return sendError("bad request");
    size_t key = b["key"] | -1;
    if (key >= db.messages.size()) return sendError("unknown message", 404);
    auto &m = db.messages[key];
    String name = b["name"] | "";
    for (auto &s : m.signals) {
        if (s.name != name.c_str()) continue;
        String kind = b["kind"] | "";
        if (kind == "auto") s.kind = s.autoKind;
        else if (kind == "manual" || kind == "const") {
            if (s.kind != dbc::Signal::Manual && s.kind != dbc::Signal::Const && b["value"].isNull()) s.manual = s.value;
            s.kind = kind == "manual" ? dbc::Signal::Manual : dbc::Signal::Const;
        }
        if (!b["value"].isNull()) s.manual = constrain((double)b["value"], s.rangeLo, s.rangeHi);
        if (!b["period"].isNull()) s.period = max(0.05, (double)b["period"]);
        return ok();
    }
    sendError("unknown signal", 404);
}

static void handleWifiGet() {
    int n = WiFi.scanNetworks();
    JsonDocument d;
    JsonArray a = d["networks"].to<JsonArray>();
    for (int i = 0; i < n && i < 20; i++) a.add(WiFi.SSID(i));
    d["ssid"] = prefs.getString("ssid", "");
    sendJson(d);
}

static void handleWifiSet() {
    JsonDocument b;
    if (!bodyJson(b)) return sendError("bad request");
    String ssid = b["ssid"] | "";
    if (ssid.isEmpty()) return sendError("SSID fehlt");
    prefs.putString("ssid", ssid);
    prefs.putString("psk", String((const char *)(b["psk"] | "")));
    ok();
    delay(500);
    ESP.restart();
}

static void uploadDone() {
    if (!uploadError.isEmpty()) return sendError(uploadError);
    if (!loadDbc(uploadName)) {
        LittleFS.remove("/dbc/" + uploadName);
        return sendError("Keine gueltige DBC: " + logLine);
    }
    ok();
}

static void uploadChunk() {
    HTTPUpload &up = server.upload();
    if (up.status == UPLOAD_FILE_START) {
        uploadError = "";
        uploadName = safeName(up.filename);
        LittleFS.mkdir("/dbc");
        uploadFile = LittleFS.open("/dbc/" + uploadName, "w");
        if (!uploadFile) uploadError = "Datei konnte nicht angelegt werden";
    } else if (up.status == UPLOAD_FILE_WRITE) {
        if (uploadFile && up.totalSize <= MAX_DBC_BYTES) uploadFile.write(up.buf, up.currentSize);
        else if (up.totalSize > MAX_DBC_BYTES) uploadError = "Datei zu gross (max 96 KB)";
    } else if (up.status == UPLOAD_FILE_END || up.status == UPLOAD_FILE_ABORTED) {
        if (uploadFile) uploadFile.close();
        if (!uploadError.isEmpty()) LittleFS.remove("/dbc/" + uploadName);
    }
}

static void handleRoot() {
    server.sendHeader("Cache-Control", "no-store");
    server.send_P(200, "text/html; charset=utf-8", (const char *)index_html_start, index_html_end - index_html_start - 1);
}

// ---------------------------------------------------------------- setup / loop
static void startWifi() {
    String ssid = prefs.getString("ssid", "");
    WiFi.setHostname(HOSTNAME);
    if (!ssid.isEmpty()) {
        WiFi.mode(WIFI_STA);
        WiFi.begin(ssid.c_str(), prefs.getString("psk", "").c_str());
        Serial.printf("WLAN %s ...", ssid.c_str());
        for (int i = 0; i < 40 && WiFi.status() != WL_CONNECTED; i++) { delay(500); Serial.print('.'); }
        Serial.println();
    }
    if (WiFi.status() != WL_CONNECTED) {
        WiFi.mode(WIFI_AP);
        WiFi.softAP(AP_NAME, AP_PASS);
        note(String("Access Point '") + AP_NAME + "' (Passwort " + AP_PASS + "), http://" + WiFi.softAPIP().toString() + "/");
    } else {
        note("WLAN verbunden: http://" + WiFi.localIP().toString() + "/  (" + HOSTNAME + ".local)");
        MDNS.begin(HOSTNAME);
    }
}

static void webTask(void *);

void setup() {
    g_lock = xSemaphoreCreateRecursiveMutex();
    Serial.begin(115200);
    delay(300);
    LittleFS.begin(true);
    LittleFS.mkdir("/dbc");
    prefs.begin("cansender", false);
    bitrate = prefs.getInt("bitrate", 500000);
    clockMHz = prefs.getInt("clock", 8);
    SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_CS);
    String last = prefs.getString("dbc", "");
    if (!last.isEmpty()) loadDbc(last);
    startWifi();

    server.on("/", HTTP_GET, handleRoot);
    server.on("/api/state", HTTP_GET, handleState);
    server.on("/api/dbc", HTTP_POST, handleDbc);
    server.on("/api/dbc/delete", HTTP_POST, handleDelete);
    server.on("/api/upload", HTTP_POST, uploadDone, uploadChunk);
    server.on("/api/connect", HTTP_POST, handleConnect);
    server.on("/api/disconnect", HTTP_POST, handleDisconnect);
    server.on("/api/all", HTTP_POST, handleAll);
    server.on("/api/message", HTTP_POST, handleMessage);
    server.on("/api/signal", HTTP_POST, handleSignal);
    server.on("/api/wifi", HTTP_GET, handleWifiGet);
    server.on("/api/wifi", HTTP_POST, handleWifiSet);
    server.begin();
    ArduinoOTA.setHostname(HOSTNAME);
    ArduinoOTA.setPassword("cansender");
    ArduinoOTA.onStart([]() { Lock lock; running = false; });   // stop sending while the firmware is replaced
    ArduinoOTA.begin();
    WiFi.setSleep(false);   // modem sleep adds up to 100s of ms of latency to every request
    xTaskCreatePinnedToCore(webTask, "web", 12288, nullptr, 1, nullptr, 0);
}

static void webTask(void *) {
    for (;;) {
        server.handleClient();
        ArduinoOTA.handle();
        delay(2);
    }
}

void loop() {
    transmit();
    delay(1);
}
