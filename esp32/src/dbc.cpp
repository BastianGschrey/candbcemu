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


// ---------------------------------------------------------------- vehicle simulation
static const double kRatio[6] = {130.0, 75.0, 50.0, 37.0, 29.0, 23.5};   // rpm per km/h in gears 1..6
static const double kAccel[6] = {13.0, 9.0, 6.5, 4.8, 3.8, 3.0};         // km/h per second at full throttle
static const double kCrankS = 1.6, kStep = 0.05;

static double clamp01(double x) { return x < 0 ? 0 : x > 1 ? 1 : x; }

void Engine::reset(double t) {
    seed_ = 7;
    bool warm = mode_ == Drive || mode_ == IdleOnly;
    t_ = t0_ = t; running_ = warm; rpm_ = warm ? 900 : 0; v_ = 0; gear_ = 1; shiftLeft_ = 0; pedal_ = 0; thr_ = 0; acc_ = 0;
    brake_ = false; phase_ = Idle; phaseLeft_ = (mode_ == Start || mode_ == IdleOnly) ? 1e9 : 4; target_ = 0; aggr_ = 0.7;
    load_ = warm ? 0.13 : 0; map_ = warm ? 0.2 : 0; coolant_ = warm ? 92 : 20; oil_ = warm ? 98 : 20;
    egt_ = warm ? 300 : 120; air_ = warm ? 35 : 20; lam_ = 0.5; wobble_ = 0;
    fan_ = false; cut_ = false;
    publish();
}

double Engine::rnd() {
    seed_ ^= seed_ << 13; seed_ ^= seed_ >> 17; seed_ ^= seed_ << 5;     // xorshift32
    return (seed_ & 0xFFFFFF) / 16777216.0;
}

double Engine::gauss(double sd) {
    double s = 0;
    for (int i = 0; i < 6; i++) s += rnd();
    return (s - 3.0) * 0.8165 * sd;                                       // ~N(0, sd)
}

void Engine::nextPhase() {
    if (phase_ == Idle || phase_ == Cruise) {
        if (phase_ == Cruise && v_ > 20 && rnd() < 0.45) {
            phase_ = Decel;
            target_ = rnd() < 0.45 ? 0.0 : v_ * (0.3 + 0.45 * rnd());
        } else if (mode_ == Start || mode_ == IdleOnly) {
            phaseLeft_ = 1e9;
            return;
        } else {
            static const double speeds[] = {30, 45, 50, 60, 80, 100, 120, 140, 165};
            phase_ = Accel;
            aggr_ = 0.35 + 0.65 * rnd();
            target_ = std::max(v_ + 15.0, speeds[(int)(rnd() * 9) % 9]);
            target_ = std::min(target_, 170.0);
        }
        phaseLeft_ = 90.0;
    } else if (phase_ == Accel) {
        phase_ = Cruise;
        phaseLeft_ = 6.0 + 19.0 * rnd();
    } else {
        if (target_ < 1.0) { phase_ = Idle; phaseLeft_ = 3.0 + 6.0 * rnd(); }
        else { phase_ = Cruise; phaseLeft_ = 3.0 + 9.0 * rnd(); }
    }
}

void Engine::drive(double dt) {
    phaseLeft_ -= dt;
    if (phase_ == Accel && (v_ >= target_ - 2.0 || phaseLeft_ <= 0)) nextPhase();
    else if (phase_ == Decel && (v_ <= target_ + 2.0 || phaseLeft_ <= 0)) nextPhase();
    else if ((phase_ == Idle || phase_ == Cruise) && phaseLeft_ <= 0) nextPhase();
    double hold = (0.00011 * v_ * v_ + 0.12) / (kAccel[gear_ - 1] * 0.8);   // pedal that keeps the speed
    brake_ = false;
    if (phase_ == Accel) pedal_ = std::min(aggr_, std::max(0.12, (target_ - v_) * 0.08 + 0.15));
    else if (phase_ == Cruise)
        pedal_ = std::min(1.0, std::max(0.0, hold + 0.06 * (target_ - v_) + 0.015 * std::sin(t_ * 0.9)));
    else if (phase_ == Decel) { pedal_ = 0.0; brake_ = v_ - target_ > 6.0 && v_ > 3.0; }
    else pedal_ = 0.0;
}

void Engine::step(double dt) {
    t_ += dt;
    if (!running_) {
        if (t_ - t0_ < kCrankS) {
            rpm_ = 200.0 + 40.0 * std::sin(t_ * 25.0);
            thr_ = pedal_ = load_ = 0.0;
            map_ = 0.35;
            return;
        }
        running_ = true;
        rpm_ = 1300.0;
    }
    drive(dt);

    double warm = clamp01((coolant_ - 20.0) / 65.0);
    double idle = 850.0 + 380.0 * (1.0 - warm);                           // cold fast idle
    wobble_ += (gauss(25.0) - wobble_) * std::min(1.0, dt * 3.0);

    // gearbox
    int g = gear_;
    double rpmInGear = v_ * kRatio[g - 1];
    shiftLeft_ = std::max(0.0, shiftLeft_ - dt);
    if (shiftLeft_ <= 0.0) {
        int n = g;
        if (v_ < 8.0) n = 1;
        else if (phase_ != Decel && rpmInGear > 2300.0 + 4200.0 * pedal_ && g < 6) n = g + 1;
        else if (g > 1 && (rpmInGear < 1350.0 || (pedal_ > 0.85 && rpmInGear < 3000.0))) n = g - 1;
        if (n != g) { gear_ = n; shiftLeft_ = n > g ? 0.35 : 0.3; }
    }
    bool shifting = shiftLeft_ > 0.0;

    // throttle plate follows the pedal; closed while shifting and at the rev limiter
    thr_ = (shifting || rpm_ > 6900.0) ? 0.0 : pedal_;
    g = gear_;
    double targetRpm = std::max(idle, v_ * kRatio[g - 1]);
    if (g == 1 && v_ < 20.0 && thr_ > 0.05)                               // slipping clutch while pulling away
        targetRpm = std::max(targetRpm, idle + thr_ * 3200.0 * (1.0 - v_ / 20.0));
    if (v_ < 3.0) targetRpm += wobble_ + 12.0 * std::sin(t_ * 0.7);
    double tau = targetRpm > rpm_ ? 0.1 : 0.28;
    rpm_ += (targetRpm - rpm_) * std::min(1.0, dt / tau);

    // vehicle
    double x = (rpm_ - 4500.0) / 4500.0;
    double tq = std::max(0.3, 0.6 + 0.4 * (1.0 - x * x));
    double engineBrake = (pedal_ < 0.05 && !shifting) ? 0.0005 * targetRpm : 0.0;
    double resist = v_ > 0.2 ? 0.00011 * v_ * v_ + 0.12 : 0.0;
    acc_ = thr_ * kAccel[g - 1] * tq - resist - engineBrake - (brake_ ? 5.5 : 0.0);
    v_ = std::max(0.0, v_ + acc_ * dt);

    // engine quantities
    double loadT = thr_ > 0.02 ? thr_ * (0.45 + 0.55 * tq) : (v_ > 5 ? 0.04 : 0.13);
    load_ += (loadT - load_) * std::min(1.0, dt / 0.15);
    cut_ = pedal_ < 0.03 && rpm_ > 1700.0 && v_ > 5.0;
    double spool = rpm_ > 2200.0 ? 1.0 : 0.3;
    double mapT = 0.16 + 0.84 * std::pow(load_, 1.5) * spool + 0.008 * std::sin(t_ * 2.1);
    map_ += (mapT - map_) * std::min(1.0, dt / (mapT > map_ ? 0.5 : 0.25));
    double heat = 92.0 + 10.0 * load_ + (v_ < 15.0 ? 6.0 : 0.0);
    if (coolant_ > 98.0) fan_ = true;
    else if (coolant_ < 93.0) fan_ = false;
    if (fan_) heat -= 9.0;
    coolant_ += (heat - coolant_) * dt / (coolant_ < 85.0 ? 100.0 : 60.0);
    oil_ += (100.0 + 12.0 * load_ - oil_) * dt / 150.0;
    egt_ += (230.0 + 650.0 * std::pow(load_, 1.2) - egt_) * dt / 2.5;
    air_ += (20.0 + 0.12 * (coolant_ - 20.0) - air_) * dt / 40.0;
    double lamT = cut_ ? 1.0 : load_ > 0.85 ? 0.33 : 0.5 + 0.05 * std::sin(t_ * 4.8);   // closed loop hunts around stoich
    lam_ += (lamT - lam_) * std::min(1.0, dt / 0.2);
}

void Engine::publish() {
    double run = running_ ? 1.0 : 0.0;
    double ripple = 0.03 * std::sin(t_ * 31.0);
    q_[R_RPM] = clamp01(rpm_ / 7000.0);
    q_[R_SPEED] = clamp01(v_ / 200.0);
    q_[R_GEAR] = (gear_ - 1) / 5.0;
    q_[R_THROTTLE] = clamp01(thr_);
    q_[R_PEDAL] = clamp01(pedal_);
    q_[R_LOAD] = clamp01(load_);
    q_[R_INJ] = clamp01(load_ * rpm_ / 5000.0);
    q_[R_MAP] = clamp01(map_);
    q_[R_TURBO] = rpm_ > 1500.0 ? clamp01((map_ - 0.2) * 1.3) : 0.0;
    q_[R_COOLANT] = clamp01((coolant_ - 20.0) / 90.0);
    q_[R_OILT] = clamp01((oil_ - 20.0) / 110.0);
    q_[R_EGT] = clamp01((egt_ - 200.0) / 750.0);
    q_[R_AIRT] = clamp01((air_ - 15.0) / 30.0);
    q_[R_VOLT] = running_ ? clamp01(0.93 + ripple - 0.1 * load_) : 0.0;
    q_[R_LAMBDA] = clamp01(lam_);
    q_[R_OILP] = clamp01(0.2 + 0.8 * rpm_ / 7000.0) * run;
    q_[R_FUELP] = clamp01(0.5 + 0.2 * (map_ - 0.2)) * run;
    q_[R_IGN] = clamp01(0.8 - 0.55 * load_ + 0.02 * std::sin(t_ * 2.0));
    q_[R_LONGG] = clamp01(0.5 + acc_ / 35.3 / 2.0 - (brake_ ? 0.1 : 0.0));
    q_[R_BRAKEP] = brake_ ? 1.0 : 0.0;
    q_[R_BRAKE] = brake_ ? 1.0 : 0.0;
    q_[R_CLUTCH] = shiftLeft_ > 0.0 ? 1.0 : 0.0;
    q_[R_NEUTRAL] = (v_ < 1.0 && phase_ == Idle) ? 1.0 : 0.0;
    q_[R_DECCUT] = cut_ ? 1.0 : 0.0;
    q_[R_FAN] = fan_ ? 1.0 : 0.0;
    q_[R_IGNSW] = run;
    q_[R_BARO] = 0.5 + 0.01 * std::sin(t_ / 60.0);
}

double Engine::get(Role role, double t) {
    if (t < t_ - 1e-9) reset();                       // time went backwards (sender restarted)
    if (t - t_ > 600.0) t_ = t - 600.0;               // do not spend ages catching up
    while (t_ + kStep <= t) step(kStep);
    publish();
    return role >= 0 && role < R_COUNT ? q_[role] : 0.0;
}

Engine &engine() {
    static Engine e;
    return e;
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


// ---- role rules (same spec syntax as _matches() in patterns.py) ----
static bool isLetter(char c) { return c >= 'a' && c <= 'z'; }

// spec: alternatives separated by '|'; an alternative is parts separated by '*' (found in that
// order); leading '^' anchors the first part at the start, leading '<' / trailing '>' require a
// non-letter before the first / after the last part.
static bool matches(const std::string &text, const std::string &spec) {
    size_t from = 0;
    while (from <= spec.size()) {
        size_t bar = spec.find('|', from);
        if (bar == std::string::npos) bar = spec.size();
        std::string alt = spec.substr(from, bar - from);
        from = bar + 1;
        bool anchored = false, before = false, after = false;
        if (!alt.empty() && alt[0] == '^') { anchored = true; alt.erase(0, 1); }
        if (!alt.empty() && alt[0] == '<') { before = true; alt.erase(0, 1); }
        if (!alt.empty() && alt.back() == '>') { after = true; alt.pop_back(); }
        std::vector<std::string> parts;
        size_t a = 0;
        for (;;) {
            size_t star = alt.find('*', a);
            parts.push_back(alt.substr(a, star == std::string::npos ? std::string::npos : star - a));
            if (star == std::string::npos) break;
            a = star + 1;
        }
        size_t pos = text.find(parts[0]);
        while (pos != std::string::npos) {
            bool ok = (!anchored || pos == 0) && (!before || pos == 0 || !isLetter(text[pos - 1]));
            size_t end = pos + parts[0].size();
            for (size_t i = 1; i < parts.size() && ok; i++) {
                size_t at = text.find(parts[i], end);
                while (i == parts.size() - 1 && after && at != std::string::npos &&
                       at + parts[i].size() < text.size() && isLetter(text[at + parts[i].size()]))
                    at = text.find(parts[i], at + 1);
                ok = at != std::string::npos;
                if (ok) end = at + parts[i].size();
            }
            if (ok && parts.size() == 1 && after && end < text.size() && isLetter(text[end])) ok = false;
            if (ok) return true;
            pos = text.find(parts[0], pos + 1);
        }
    }
    return false;
}

struct RoleRule { const char *spec; Role role; double p0, p1, f0, f1; bool whole; };

static const char *kSkip = "slip|wheeldiff|differential|cruise|tqmgmt|gearswitch|ethanol";
static const RoleRule kRoles[] = {
    {"brake*switch|brake*pedal", R_BRAKE, 0, 1, 0.0, 1.0, true},
    {"^clutch|clutchswitch", R_CLUTCH, 0, 1, 0.0, 1.0, true},
    {"neutral", R_NEUTRAL, 0, 1, 0.0, 1.0, true},
    {"decel*cut|gearcut", R_DECCUT, 0, 1, 0.0, 1.0, true},
    {"fan", R_FAN, 0, 1, 0.0, 1.0, true},
    {"ignitionswitch", R_IGNSW, 0, 1, 0.0, 1.0, true},
    {"turbo*speed|turbo*rpm", R_TURBO, 0, 150000, 0.0, 0.8, false},
    {"driveshaft", R_SPEED, 0, 6400, 0.0, 0.6, false},
    {"enginespeed|<rpm|drehzahl", R_RPM, 0, 7000, 0.0, 0.7, false},
    {"oil*press|oilp>|gearbox*press", R_OILP, 0, 6, 0.1, 0.7, false},
    {"fuel*press|fuelp>|coolant*press", R_FUELP, 0, 6, 0.35, 0.6, false},
    {"brake*press", R_BRAKEP, 0, 100, 0.0, 0.8, false},
    {"baro", R_BARO, 95, 105, 0.45, 0.55, false},
    {"boost|manifold|<map>|kpa|wastegate|press|<bar>|psi", R_MAP, 30, 180, 0.1, 0.7, false},
    {"coolant|water|clt|enginetemp", R_COOLANT, 20, 110, 0.35, 0.8, false},
    {"oil*temp|oilt|gearbox*temp|diff*temp", R_OILT, 20, 130, 0.3, 0.75, false},
    {"egt", R_EGT, 200, 950, 0.15, 0.8, false},
    {"air*temp|iat|intake|fuel*temp|temp", R_AIRT, 15, 45, 0.25, 0.5, false},
    {"wheelspeed|speed|vspd|geschw|kph|km/h|mph", R_SPEED, 0, 200, 0.0, 0.6, false},
    {"gear|gang", R_GEAR, 1, 6, 0.14, 0.86, true},
    {"throttle|tps|dbw", R_THROTTLE, 0, 100, 0.0, 0.9, false},
    {"pedal|accel", R_PEDAL, 0, 100, 0.0, 0.9, false},
    {"load|demand", R_LOAD, 0, 100, 0.0, 0.9, false},
    {"fuelflow|duty|inj", R_INJ, 0, 100, 0.0, 0.8, false},
    {"ign*ang|timing|advance", R_IGN, 5, 40, 0.2, 0.8, false},
    {"afr", R_LAMBDA, 11.0, 18.4, 0.3, 0.7, false},
    {"lambda|lamnda|o2", R_LAMBDA, 0.75, 1.25, 0.3, 0.7, false},
    {"batt|volt", R_VOLT, 11.5, 14.5, 0.62, 0.7, false},
    {"longitudinal", R_LONGG, -1, 1, 0.35, 0.65, false},
};

// Gives `s` the simulation kind if its name says it is an engine quantity. Returns false if not.
static bool assignRole(Signal &s, const std::string &name, const std::string &unit, bool oneBit) {
    std::string text;
    for (unsigned char c : name) if (c != '_' && c != ' ') text += (char)std::tolower(c);
    text += ' ';
    for (unsigned char c : unit) text += (char)std::tolower(c);
    if (matches(text, kSkip)) return false;
    for (const auto &r : kRoles) {
        if (!matches(text, r.spec)) continue;
        if (oneBit && !r.whole) return false;       // a flag that merely mentions a quantity
        bool kpa = text.find("kpa") != std::string::npos || text.find("kilopascal") != std::string::npos;
        bool isTemp = r.role == R_COOLANT || r.role == R_OILT || r.role == R_EGT || r.role == R_AIRT;
        double p0 = r.p0, p1 = r.p1;
        if (r.role == R_SPEED && p1 > 200 && text.find("rpm") == std::string::npos) p1 = 200;
        if (r.role == R_SPEED && text.find("mph") != std::string::npos) p1 = 125;
        if ((r.role == R_OILP || r.role == R_FUELP) && kpa) p1 *= 100;
        if (isTemp && text.find("kelvin") != std::string::npos) { p0 += 273.15; p1 += 273.15; }
        else if (isTemp && text.find("fahrenheit") != std::string::npos) { p0 = p0 * 1.8 + 32; p1 = p1 * 1.8 + 32; }
        double span = s.rangeHi - s.rangeLo;
        if (s.rangeLo <= p0 && p1 <= s.rangeHi + 1e-9 && !(r.role == R_MAP && !kpa)) {
            s.lo = p0; s.hi = p1;
        } else {
            s.lo = s.rangeLo + r.f0 * span; s.hi = s.rangeLo + r.f1 * span;
        }
        s.kind = Signal::Sim;
        s.role = r.role;
        s.quantize = r.whole;
        s.period = 10;
        return true;
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
            bool oneBit = span <= 1.0 && s.length <= 2;
            std::string squeezed;
            for (unsigned char c : s.name) if (c != '_' && c != ' ') squeezed += (char)std::tolower(c);
            if ((s.choices.empty() || s.isMuxSwitch || matches(squeezed, "^gear|gang")) &&
                assignRole(s, s.name, s.unit, oneBit)) {
                // driven by the vehicle simulation
            } else if (!s.choices.empty() && !s.isMuxSwitch) {
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
    if (s.kind == Signal::Sim) {
        double u = engine().get(s.role, t);
        if (s.role == R_EGT || s.role == R_LAMBDA || s.role == R_INJ)      // not all of a kind identical
            u = clamp01(u + (s.phase - 0.5) * 0.06);
        double v = s.lo + u * (s.hi - s.lo);
        if (!s.choices.empty()) {                                           // enumeration: nearest listed value
            double best = v, bd = 1e300;
            for (auto &c : s.choices) {
                double pv = (double)c.first * s.factor + s.offset;
                if (std::fabs(pv - v) < bd) { bd = std::fabs(pv - v); best = pv; }
            }
            return best;
        }
        return s.quantize ? std::nearbyint(v) : v;
    }
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
