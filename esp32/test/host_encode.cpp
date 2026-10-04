// Host test helper: reads a DBC, sets every signal to a physical value given on the command line
// (mode "min" | "max" | "mid"), prints "<id hex> <data hex>" per message. Compared with cantools by test/compare.py.
#include <cstdio>
#include <fstream>
#include <sstream>
#include "../src/dbc.h"

int main(int argc, char **argv) {
    if (argc < 3) return 2;
    std::ifstream f(argv[1]);
    std::stringstream ss;
    ss << f.rdbuf();
    dbc::Database db;
    std::string err;
    if (!dbc::parse(ss.str(), db, err)) { fprintf(stderr, "parse: %s\n", err.c_str()); return 1; }
    std::string mode = argv[2];
    for (auto &m : db.messages) {
        if (mode == "info") {
            printf("%X %s %d %d", m.id, m.name.c_str(), m.dlc, m.cycleMs);
            for (auto &s : m.signals) printf(" %s:%.6g:%.6g", s.name.c_str(), dbc::signalRangeLo(s), dbc::signalRangeHi(s));
            printf("\n");
            continue;
        }
        for (auto &s : m.signals) {
            double lo = dbc::signalRangeLo(s), hi = dbc::signalRangeHi(s);
            s.value = mode == "min" ? lo : mode == "max" ? hi : (lo + hi) / 2;
            // keep multiplexer switch values from the message's own list
        }
        uint8_t d[8];
        dbc::encode(m, d);
        printf("%X", m.id);
        for (int i = 0; i < m.dlc; i++) printf(" %02X", d[i]);
        printf("\n");
    }
    return 0;
}
