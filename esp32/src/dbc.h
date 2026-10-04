// Minimal DBC reader + frame encoder + demo value curves. Plain C++ (no Arduino), so it can be
// tested on the PC against cantools (see test/).
#pragma once
#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace dbc {

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
    enum Kind { Sine, Triangle, Square, Const, Choices, Manual };
    Kind kind = Sine, autoKind = Sine;
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
