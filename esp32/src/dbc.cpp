#include "dbc.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>

namespace dbc {

// ---------------------------------------------------------------- parsing helpers
static std::string trim(const std::string &s) {
    size_t a = 0, b = s.size();
    while (a < b && std::isspace((unsigned char)s[a])) a++;
    while (b > a && std::isspace((unsigned char)s[b - 1])) b--;
    return s.substr(a, b - a);
}

static bool startsWith(const std::string &s, const char *p) { return s.compare(0, strlen(p), p) == 0; }

// next whitespace separated token starting at pos
static std::string token(const std::string &s, size_t &pos) {
    while (pos < s.size() && std::isspace((unsigned char)s[pos])) pos++;
    size_t a = pos;
    while (pos < s.size() && !std::isspace((unsigned char)s[pos])) pos++;
    return s.substr(a, pos - a);
}

static Message *findMessage(Database &db, uint32_t id) {
    for (auto &m : db.messages)
        if (m.id == id) return &m;
    return nullptr;
}

static uint32_t rawIdToId(unsigned long long raw, bool &ext) {
    ext = (raw & 0x80000000ULL) != 0;
    return (uint32_t)(raw & 0x1FFFFFFFULL);
}

static void parseMessageLine(const std::string &line, Database &db) {
    // BO_ <id> <name>: <dlc> <sender>
    size_t pos = 3;
    std::string idS = token(line, pos);
    std::string nameS = token(line, pos);
    std::string dlcS = token(line, pos);
    if (idS.empty() || nameS.empty() || dlcS.empty()) return;
    if (nameS.back() == ':') nameS.pop_back();
    Message m;
    m.id = rawIdToId(strtoull(idS.c_str(), nullptr, 10), m.extended);
    m.name = nameS;
    m.dlc = std::min(8, std::max(0, atoi(dlcS.c_str())));
    db.messages.push_back(m);
}

static void parseSignalLine(const std::string &line, Message &m) {
    // SG_ <name> [M|mN|mNM] : <start>|<len>@<order><sign> (<factor>,<offset>) [<min>|<max>] "<unit>" <rx>
    size_t pos = 3;
    Signal s;
    s.name = token(line, pos);
    std::string t = token(line, pos);
    if (t != ":") {              // multiplexer indicator
        if (t == "M") s.isMuxSwitch = true;
        else if (t[0] == 'm') {
            s.muxValue = atoi(t.c_str() + 1);
            if (t.back() == 'M') s.isMuxSwitch = true;
        }
        t = token(line, pos);    // the ":"
    }
    size_t colon = line.find(':', 3);
    if (colon == std::string::npos) return;
    const char *p = line.c_str() + colon + 1;
    int start = 0, len = 0, order = 1;
    char sign = '+';
    int n = 0;
    if (sscanf(p, " %d|%d@%d%c%n", &start, &len, &order, &sign, &n) < 4) return;
    p += n;
    double factor = 1, offset = 0, mn = 0, mx = 0;
    if (sscanf(p, " (%lf,%lf)%n", &factor, &offset, &n) < 2) return;
    p += n;
    if (sscanf(p, " [%lf|%lf]%n", &mn, &mx, &n) < 2) return;
    p += n;
    s.start = start;
    s.length = std::min(64, std::max(1, len));
    s.littleEndian = order == 1;
    s.isSigned = sign == '-';
    s.factor = factor == 0 ? 1 : factor;
    s.offset = offset;
    s.minimum = mn;
    s.maximum = mx;
    const char *q1 = strchr(p, '"');
    const char *q2 = q1 ? strchr(q1 + 1, '"') : nullptr;
    if (q1 && q2) s.unit.assign(q1 + 1, q2 - q1 - 1);
    m.signals.push_back(s);
}

static void parseValLine(const std::string &line, Database &db) {
    // VAL_ <id> <signal> <n> "text" <n> "text" ... ;
    size_t pos = 4;
    std::string idS = token(line, pos);
    std::string sigS = token(line, pos);
    bool ext;
    Message *m = findMessage(db, rawIdToId(strtoull(idS.c_str(), nullptr, 10), ext));
    if (!m) return;
    for (auto &s : m->signals) {
        if (s.name != sigS) continue;
        const char *p = line.c_str() + pos;
        while (*p) {
            while (*p && std::isspace((unsigned char)*p)) p++;
            if (*p == ';' || !*p) break;
            char *end;
            long v = strtol(p, &end, 10);
            if (end == p) break;
            p = end;
            const char *q1 = strchr(p, '"');
            const char *q2 = q1 ? strchr(q1 + 1, '"') : nullptr;
            if (!q1 || !q2) break;
            s.choices.push_back({v, std::string(q1 + 1, q2 - q1 - 1)});
            p = q2 + 1;
        }
    }
}

// DBC files are usually Latin-1 / Windows-1252 ("°C" is a single 0xB0 byte). The web UI and JSON want
// UTF-8, so convert unless the text already is valid UTF-8.
static bool validUtf8(const std::string &t) {
    size_t i = 0;
    while (i < t.size()) {
        unsigned char c = t[i];
        int n = c < 0x80 ? 0 : (c >> 5) == 6 ? 1 : (c >> 4) == 14 ? 2 : (c >> 3) == 30 ? 3 : -1;
        if (n < 0) return false;
        for (int k = 1; k <= n; k++)
            if (i + k >= t.size() || (t[i + k] & 0xC0) != 0x80) return false;
        i += n + 1;
    }
    return true;
}

std::string toUtf8(const std::string &t) {
    if (validUtf8(t)) return t;
    std::string out;
    out.reserve(t.size() + 16);
    for (unsigned char c : t) {
        if (c < 0x80) out += (char)c;
        else { out += (char)(0xC0 | (c >> 6)); out += (char)(0x80 | (c & 0x3F)); }
    }
    return out;
}

bool parse(const std::string &rawText, Database &db, std::string &error) {
    const std::string text = toUtf8(rawText);
    db.messages.clear();
    size_t i = 0;
    while (i <= text.size()) {
        size_t j = text.find('\n', i);
        if (j == std::string::npos) j = text.size();
        std::string line = trim(text.substr(i, j - i));
        i = j + 1;
        if (line.empty()) continue;
        if (startsWith(line, "BO_ ")) {
            parseMessageLine(line, db);
        } else if (startsWith(line, "SG_ ")) {
            if (!db.messages.empty()) parseSignalLine(line, db.messages.back());
        } else if (startsWith(line, "VAL_ ")) {
            parseValLine(line, db);
        } else if (startsWith(line, "BA_ \"GenMsgCycleTime\" BO_ ")) {
            size_t pos = strlen("BA_ \"GenMsgCycleTime\" BO_ ");
            std::string idS = token(line, pos);
            std::string msS = token(line, pos);
            if (!msS.empty() && msS.back() == ';') msS.pop_back();
            bool ext;
            Message *m = findMessage(db, rawIdToId(strtoull(idS.c_str(), nullptr, 10), ext));
            if (m && atoi(msS.c_str()) > 0) m->cycleMs = atoi(msS.c_str());
        }
    }
    // drop the vector's "VECTOR__INDEPENDENT_SIG_MSG" pseudo message and empty ones
    db.messages.erase(std::remove_if(db.messages.begin(), db.messages.end(),
                                     [](const Message &m) { return m.signals.empty(); }),
                      db.messages.end());
    for (auto &m : db.messages) {
        m.cycleMs = std::min(3600000, std::max(5, m.cycleMs));
        for (size_t k = 0; k < m.signals.size(); k++) {
            const Signal &s = m.signals[k];
            if (s.isMuxSwitch && s.muxValue < 0) m.muxSwitch = (int)k;
        }
        if (m.muxSwitch >= 0) {
            for (auto &s : m.signals)
                if (s.muxValue >= 0 && std::find(m.muxValues.begin(), m.muxValues.end(), s.muxValue) == m.muxValues.end())
                    m.muxValues.push_back(s.muxValue);
            std::sort(m.muxValues.begin(), m.muxValues.end());
            if (m.muxValues.empty()) m.muxSwitch = -1;
        }
    }
    if (db.messages.empty()) {
        error = "no messages with signals found";
        return false;
    }
    buildProfiles(db);
    return true;
}

// ---------------------------------------------------------------- ranges + curves
static void rawLimits(const Signal &s, double &lo, double &hi) {
    if (s.isSigned) {
        lo = -std::ldexp(1.0, s.length - 1);
        hi = std::ldexp(1.0, s.length - 1) - 1;
    } else {
        lo = 0;
        hi = std::ldexp(1.0, s.length) - 1;
    }
}

double signalRangeLo(const Signal &s) { return s.rangeLo; }
double signalRangeHi(const Signal &s) { return s.rangeHi; }

static void computeRange(Signal &s) {
    double rl, rh;
    rawLimits(s, rl, rh);
    double a = rl * s.factor + s.offset, b = rh * s.factor + s.offset;
    double repLo = std::min(a, b), repHi = std::max(a, b);
    double lo = repLo, hi = repHi;
    if (s.minimum != s.maximum) {        // the DBC declares a range
        lo = std::max(lo, std::min(s.minimum, s.maximum));
        hi = std::min(hi, std::max(s.minimum, s.maximum));
        if (lo >= hi) { lo = repLo; hi = repHi; }
    }
    if (hi - lo > 1.0e5 || hi <= lo) {
        if (hi > lo) hi = lo + 100.0;    // unbounded integer: keep a sane window above the lower limit
        else { lo = 0; hi = 100; }
    }
    s.rangeLo = lo;
    s.rangeHi = hi;
}

static uint32_t crc32(const std::string &s) {
    uint32_t c = 0xFFFFFFFFu;
    for (unsigned char ch : s) {
        c ^= ch;
        for (int k = 0; k < 8; k++) c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1u)));
    }
    return ~c;
}

static bool has(const std::string &text, const char *words) {
    // words: '|' separated alternatives
    std::string w(words);
    size_t i = 0;
    while (i <= w.size()) {
        size_t j = w.find('|', i);
        if (j == std::string::npos) j = w.size();
        if (j > i && text.find(w.substr(i, j - i)) != std::string::npos) return true;
        i = j + 1;
    }
    return false;
}

void buildProfiles(Database &db) {
    for (auto &m : db.messages) {
        for (auto &s : m.signals) {
            computeRange(s);
            s.phase = (crc32(s.name) % 1000) / 1000.0;
            double span = s.rangeHi - s.rangeLo;
            std::string text = s.name + " " + s.unit;
            std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) { return std::tolower(c); });

            struct { const char *words; Signal::Kind kind; double period, f0, f1; } rules[] = {
                {"rpm|drehzahl|enginespeed", Signal::Sine, 9, 0.12, 0.85},
                {"speed|geschw|kph|km/h|mph", Signal::Triangle, 40, 0.0, 0.6},
                {"temp|\xC2\xB0""c|degc|celsius|coolant|water|egt", Signal::Triangle, 80, 0.35, 0.8},
                {"throttle|tps|pedal|accel|load|duty|inj", Signal::Triangle, 11, 0.02, 0.9},
                {"boost|map|manifold|press|kpa|bar|psi", Signal::Sine, 7, 0.1, 0.7},
                {"batt|volt", Signal::Sine, 30, 0.62, 0.7},
                {"lambda|afr|o2", Signal::Sine, 5, 0.45, 0.6},
                {"gear|gang", Signal::Triangle, 36, 0.14, 0.86},
                {"flag|status|warn|alarm|light|lamp|switch|state", Signal::Square, 12, 0.0, 1.0},
            };
            s.kind = Signal::Sine;
            s.period = 10;
            s.lo = s.rangeLo + 0.1 * span;
            s.hi = s.rangeLo + 0.9 * span;
            if (!s.choices.empty() && !s.isMuxSwitch) {
                s.kind = Signal::Choices;
                s.period = 7;
                s.lo = s.rangeLo;
                s.hi = s.rangeHi;
            } else if (span <= 1.0 && s.length <= 2) {
                s.kind = Signal::Square;
                s.period = 12;
                s.lo = s.rangeLo;
                s.hi = s.rangeHi;
            } else {
                for (auto &r : rules) {
                    if (has(text, r.words)) {
                        s.quantize = std::string(r.words).compare(0, 4, "gear") == 0;
                        s.kind = r.kind;
                        s.period = r.period;
                        s.lo = s.rangeLo + r.f0 * span;
                        s.hi = s.rangeLo + r.f1 * span;
                        break;
                    }
                }
            }
            s.autoKind = s.kind;
            s.defLo = s.lo;
            s.defHi = s.hi;
            s.manual = s.lo;
            s.value = s.lo;
        }
    }
}

double evaluate(const Signal &s, double t) {
    if (s.kind == Signal::Const || s.kind == Signal::Manual) return s.manual;
    if (s.kind == Signal::Choices) {
        if (s.choices.empty()) return s.manual;
        size_t n = s.choices.size();
        size_t idx = (size_t)(t / std::max(s.period, 0.1) + s.phase * n) % n;
        return (double)s.choices[idx].first * s.factor + s.offset;
    }
    double period = std::max(s.period, 0.05);
    double x = std::fmod(t / period + s.phase, 1.0);
    double unit;
    if (s.kind == Signal::Triangle) unit = 1.0 - std::fabs(2.0 * x - 1.0);
    else if (s.kind == Signal::Square) unit = x >= 0.5 ? 1.0 : 0.0;
    else unit = 0.5 - 0.5 * std::cos(2.0 * M_PI * x);
    double v = s.lo + unit * (s.hi - s.lo);
    return s.quantize ? std::nearbyint(v) : v;
}

// ---------------------------------------------------------------- encoding
// Bits are only ever set (like cantools): signals that overlap in a DBC are OR-ed together.
static void putBits(uint8_t *data, const Signal &s, uint64_t raw) {
    if (s.littleEndian) {
        for (int i = 0; i < s.length; i++) {
            int pos = s.start + i;
            if (pos / 8 >= 8) break;
            if ((raw >> i) & 1) data[pos / 8] |= (uint8_t)(1u << (pos % 8));
        }
    } else {
        int pos = s.start;                      // MSB first, DBC "sawtooth" numbering
        for (int i = s.length - 1; i >= 0; i--) {
            if (pos / 8 < 8) {
                if ((raw >> i) & 1) data[pos / 8] |= (uint8_t)(1u << (pos % 8));
            }
            pos = (pos % 8 == 0) ? pos + 15 : pos - 1;
        }
    }
}

static uint64_t toRaw(const Signal &s, double physical) {
    double lo, hi;
    rawLimits(s, lo, hi);
    double r = std::nearbyint((physical - s.offset) / s.factor);
    r = std::min(hi, std::max(lo, r));
    int64_t v = (int64_t)r;
    uint64_t mask = s.length >= 64 ? ~0ULL : ((1ULL << s.length) - 1);
    return (uint64_t)v & mask;
}

void encode(const Message &m, uint8_t *out) {
    memset(out, 0, 8);
    int mux = m.muxSwitch >= 0 && !m.muxValues.empty() ? m.muxValues[m.muxIndex % m.muxValues.size()] : -1;
    for (size_t k = 0; k < m.signals.size(); k++) {
        const Signal &s = m.signals[k];
        if ((int)k == m.muxSwitch) {
            Signal sw = s;
            putBits(out, sw, (uint64_t)toRaw(s, mux * s.factor + s.offset));
            continue;
        }
        if (s.muxValue >= 0 && s.muxValue != mux) continue;
        putBits(out, s, toRaw(s, s.value));
    }
}

void step(Message &m, double t) {
    if (m.muxSwitch >= 0 && !m.muxValues.empty()) m.muxIndex = (m.muxIndex + 1) % m.muxValues.size();
    for (auto &s : m.signals) s.value = evaluate(s, t);
}

}  // namespace dbc
