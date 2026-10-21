// Minimal DBC reader + frame encoder + demo values (curves and an endless engine simulation). Plain C++ (no Arduino), so it can be
// tested on the PC against cantools (see test/).
#pragma once
#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace dbc {

// Quantities of the simulated vehicle that signals can follow (by name, see buildProfiles).
enum Role {
    R_NONE = -1, R_RPM, R_SPEED, R_GEAR, R_THROTTLE, R_PEDAL, R_LOAD, R_INJ, R_MAP, R_TURBO, R_COOLANT,
    R_OILT, R_EGT, R_AIRT, R_VOLT, R_LAMBDA, R_OILP, R_FUELP, R_IGN, R_LONGG, R_BRAKEP, R_BRAKE,
    R_CLUTCH, R_NEUTRAL, R_DECCUT, R_FAN, R_IGNSW, R_BARO, R_COUNT
};

// Endless vehicle simulation: cold start, idle, pulling away, shifting, cruising, braking to a
// stop, and again. get() returns a quantity as 0..1 of its reference range; the simulation
// advances by itself to the requested time. Same model as Engine in candbcemu/headless/patterns.py.
class Engine {
public:
    Engine() { reset(); }
    // Scenarios: cold start + driving, cold start + idle only, warm + driving, warm idle (stays at idle).
    enum Mode { Complete, Start, Drive, IdleOnly };
    Mode mode() const { return mode_; }
    void setMode(Mode m, double t) { mode_ = m; reset(t); }   // and start it over at time t
    void reset(double t = 0.0);
    double get(Role role, double t);

private:
    void step(double dt);
    void drive(double dt);
    void nextPhase();
    void publish();
    double rnd();                       // 0..1
    double gauss(double sd);
    uint32_t seed_ = 7;
    Mode mode_ = Complete;
    double t0_ = 0;

    enum Phase { Idle, Accel, Cruise, Decel };
    double t_ = 0;
    bool running_ = false;
    double rpm_ = 0, v_ = 0;
    int gear_ = 1;
    double shiftLeft_ = 0, pedal_ = 0, thr_ = 0, acc_ = 0;
    bool brake_ = false;
    Phase phase_ = Idle;
    double phaseLeft_ = 4, target_ = 0, aggr_ = 0.7;
    double load_ = 0, map_ = 0, coolant_ = 20, oil_ = 20, egt_ = 120, air_ = 20, lam_ = 0.5, wobble_ = 0;
    bool fan_ = false, cut_ = false;
    double q_[R_COUNT] = {0};
};

struct Signal {
    std::string name;
    std::string unit;
    int start = 0;            // DBC start bit
    int length = 1;
    bool littleEndian = true; // @1 = Intel, @0 = Motorola
    bool isSigned = false;
    double factor = 1.0, offset = 0.0;
    double minimum = 0.0, maximum = 0.0;   // as in the file; min == max means "not set"
    int muxValue = -1;        // >= 0: only present when the multiplexer switch has this value
    bool isMuxSwitch = false;
    std::vector<std::pair<long, std::string>> choices;   // raw value -> text

    // ---- demo curve state (filled by buildProfiles) ----
    enum Kind { Sine, Triangle, Square, Const, Choices, Manual, Sim };
    Kind kind = Sine, autoKind = Sine;
    Role role = R_NONE;                      // kind Sim: the simulated quantity that drives the signal
    double period = 10.0, lo = 0.0, hi = 1.0, phase = 0.0, manual = 0.0;
    bool quantize = false;                   // whole numbers only (gear): the curve steps
    double defLo = 0.0, defHi = 1.0;         // the automatic min/max, to go back to
    double rangeLo = 0.0, rangeHi = 1.0;    // hard limits (DBC range cut to what the bits can hold)
    double value = 0.0;                      // last value used
};

struct Message {
    uint32_t id = 0;
    bool extended = false;
    std::string name;
    int dlc = 8;
    int cycleMs = 100;
    std::vector<Signal> signals;
    int muxSwitch = -1;                  // index in signals, -1 if none
    std::vector<int> muxValues;          // distinct values the switch takes
    size_t muxIndex = 0;
    // run-time
    bool enabled = false;
    uint32_t sent = 0, errors = 0;
    unsigned long nextDue = 0;
    uint8_t lastData[8] = {0};
};

struct Database {
    std::vector<Message> messages;
};

// Latin-1 -> UTF-8 unless the text already is valid UTF-8 (parse() does this itself).
std::string toUtf8(const std::string &text);

// Parses DBC text. Returns false (and a reason in `error`) if nothing usable was found.
bool parse(const std::string &text, Database &db, std::string &error);

// Chooses a demo curve for every signal (from range, name and unit) and the hard limits.
void buildProfiles(Database &db);

// The shared vehicle simulation; reset it when sending starts (key on).
Engine &engine();

// Value of the signal's curve at time t (seconds).
double evaluate(const Signal &s, double t);

// Fills `out` (dlc bytes) from the signals' current `value`s. For multiplexed messages only the
// signals of the selected branch are written (mux switch value = msg.muxValues[msg.muxIndex]).
void encode(const Message &m, uint8_t *out);

// Computes every signal's value at time t (and advances the multiplexer). Then call encode().
void step(Message &m, double t);

double signalRangeLo(const Signal &s);
double signalRangeHi(const Signal &s);

}  // namespace dbc
