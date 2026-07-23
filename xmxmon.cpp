// Copyright 2026 the xmxmon authors
// SPDX-License-Identifier: Apache-2.0
//
// xmxmon — standalone Level Zero metrics observer for Intel Arc GPUs.
//
// Samples a hardware metric group (e.g. ComputeBasic, VectorEngineProfile)
// device-wide via zetMetricStreamerOpen, without wrapping or touching the
// workload process. Emits incremental CSV (one row per HW report) and a
// summary on exit. SIGINT/SIGTERM safe: data is written as it arrives.
//
// Build: g++ -O2 -o xmxmon xmxmon.cpp -lze_loader
// Run:   ZET_ENABLE_METRICS=1 ./xmxmon --list
//        ZET_ENABLE_METRICS=1 ./xmxmon --device 1 --group ComputeBasic \
//            --period-ms 100 --duration 60 --out run.csv

#include <level_zero/ze_api.h>
#include <level_zero/zet_api.h>

#include <atomic>
#include <chrono>
#include <cinttypes>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

static std::atomic<bool> g_stop{false};
static void on_signal(int) { g_stop = true; }

#define ZE_CHECK(call)                                                        \
    do {                                                                      \
        ze_result_t _r = (call);                                              \
        if (_r != ZE_RESULT_SUCCESS) {                                        \
            fprintf(stderr, "FATAL: %s failed: 0x%x\n", #call, (unsigned)_r); \
            exit(2);                                                          \
        }                                                                     \
    } while (0)

struct Options {
    bool     list        = false;
    bool     list_metrics = false;   // with --group: dump that group's metrics
    bool     json        = false;    // ndjson lines instead of CSV
    int      device      = -1;       // required for sampling
    std::string group    = "ComputeBasic";
    double   period_ms   = 100.0;    // HW sampling period
    double   duration_s  = 0.0;      // 0 = until SIGINT
    std::string out;                 // empty = stdout
    double   read_ms     = 500.0;    // how often we drain the streamer
};

static void usage(const char* argv0) {
    fprintf(stderr,
        "usage: %s [--list] [--list-metrics --group G] [--device N --group G\n"
        "          [--period-ms F] [--duration S] [--read-ms F] [--out FILE]]\n"
        "\n"
        "  --list           enumerate devices and their time-based metric groups\n"
        "  --list-metrics   with --group: list the metrics inside that group\n"
        "  --device N       device index (see --list)\n"
        "  --group G        metric group name (default ComputeBasic)\n"
        "  --period-ms F    HW sampling period in ms (default 100)\n"
        "  --duration S     stop after S seconds (default: run until SIGINT)\n"
        "  --read-ms F      streamer drain interval in ms (default 500)\n"
        "  --out FILE       CSV output file (default stdout)\n",
        argv0);
}

static std::vector<ze_device_handle_t> get_devices(ze_driver_handle_t& drv_out) {
    uint32_t ndrv = 0;
    ZE_CHECK(zeDriverGet(&ndrv, nullptr));
    if (ndrv == 0) { fprintf(stderr, "FATAL: no Level Zero drivers\n"); exit(2); }
    std::vector<ze_driver_handle_t> drivers(ndrv);
    ZE_CHECK(zeDriverGet(&ndrv, drivers.data()));

    // Pick the driver that actually has GPU devices.
    for (auto drv : drivers) {
        uint32_t ndev = 0;
        ZE_CHECK(zeDeviceGet(drv, &ndev, nullptr));
        if (ndev == 0) continue;
        std::vector<ze_device_handle_t> devs(ndev);
        ZE_CHECK(zeDeviceGet(drv, &ndev, devs.data()));
        std::vector<ze_device_handle_t> gpus;
        for (auto d : devs) {
            ze_device_properties_t p{ZE_STRUCTURE_TYPE_DEVICE_PROPERTIES};
            ZE_CHECK(zeDeviceGetProperties(d, &p));
            if (p.type == ZE_DEVICE_TYPE_GPU) gpus.push_back(d);
        }
        if (!gpus.empty()) { drv_out = drv; return gpus; }
    }
    fprintf(stderr, "FATAL: no GPU devices found\n");
    exit(2);
}

static std::vector<zet_metric_group_handle_t> get_time_groups(ze_device_handle_t dev) {
    uint32_t n = 0;
    ZE_CHECK(zetMetricGroupGet(dev, &n, nullptr));
    std::vector<zet_metric_group_handle_t> all(n);
    if (n) ZE_CHECK(zetMetricGroupGet(dev, &n, all.data()));
    std::vector<zet_metric_group_handle_t> out;
    for (auto g : all) {
        zet_metric_group_properties_t p{ZET_STRUCTURE_TYPE_METRIC_GROUP_PROPERTIES};
        ZE_CHECK(zetMetricGroupGetProperties(g, &p));
        if (p.samplingType & ZET_METRIC_GROUP_SAMPLING_TYPE_FLAG_TIME_BASED)
            out.push_back(g);
    }
    return out;
}

struct MetricInfo {
    std::string name;
    std::string description;
    zet_value_type_t type;
};

static std::vector<MetricInfo> get_metrics(zet_metric_group_handle_t grp) {
    uint32_t n = 0;
    ZE_CHECK(zetMetricGet(grp, &n, nullptr));
    std::vector<zet_metric_handle_t> hs(n);
    if (n) ZE_CHECK(zetMetricGet(grp, &n, hs.data()));
    std::vector<MetricInfo> out;
    for (auto h : hs) {
        zet_metric_properties_t p{ZET_STRUCTURE_TYPE_METRIC_PROPERTIES};
        ZE_CHECK(zetMetricGetProperties(h, &p));
        out.push_back({p.name, p.description, p.resultType});
    }
    return out;
}

static double value_as_double(const zet_typed_value_t& v) {
    switch (v.type) {
        case ZET_VALUE_TYPE_UINT32:  return (double)v.value.ui32;
        case ZET_VALUE_TYPE_UINT64:  return (double)v.value.ui64;
        case ZET_VALUE_TYPE_FLOAT32: return (double)v.value.fp32;
        case ZET_VALUE_TYPE_FLOAT64: return v.value.fp64;
        case ZET_VALUE_TYPE_BOOL8:   return v.value.b8 ? 1.0 : 0.0;
        default:                     return NAN;
    }
}

int main(int argc, char** argv) {
    Options opt;
    for (int i = 1; i < argc; i++) {
        std::string a = argv[i];
        auto next = [&]() -> const char* {
            if (i + 1 >= argc) { usage(argv[0]); exit(1); }
            return argv[++i];
        };
        if      (a == "--list")         opt.list = true;
        else if (a == "--list-metrics") opt.list_metrics = true;
        else if (a == "--device")       opt.device = atoi(next());
        else if (a == "--group")        opt.group = next();
        else if (a == "--period-ms")    opt.period_ms = atof(next());
        else if (a == "--duration")     opt.duration_s = atof(next());
        else if (a == "--read-ms")      opt.read_ms = atof(next());
        else if (a == "--out")          opt.out = next();
        else if (a == "--json")         opt.json = true;
        else { usage(argv[0]); return 1; }
    }

    setenv("ZET_ENABLE_METRICS", "1", 0);  // must be set before zeInit
    ZE_CHECK(zeInit(ZE_INIT_FLAG_GPU_ONLY));

    ze_driver_handle_t driver{};
    auto devices = get_devices(driver);

    if (opt.list) {
        for (size_t i = 0; i < devices.size(); i++) {
            ze_device_properties_t p{ZE_STRUCTURE_TYPE_DEVICE_PROPERTIES};
            ZE_CHECK(zeDeviceGetProperties(devices[i], &p));
            printf("device %zu: %s\n", i, p.name);
            for (auto g : get_time_groups(devices[i])) {
                zet_metric_group_properties_t gp{ZET_STRUCTURE_TYPE_METRIC_GROUP_PROPERTIES};
                ZE_CHECK(zetMetricGroupGetProperties(g, &gp));
                printf("  %-28s %3u metrics  %s\n", gp.name, gp.metricCount, gp.description);
            }
        }
        return 0;
    }

    if (opt.device < 0 || opt.device >= (int)devices.size()) {
        fprintf(stderr, "ERROR: --device required (0..%zu), see --list\n",
                devices.size() - 1);
        return 1;
    }
    ze_device_handle_t dev = devices[opt.device];

    zet_metric_group_handle_t group = nullptr;
    for (auto g : get_time_groups(dev)) {
        zet_metric_group_properties_t gp{ZET_STRUCTURE_TYPE_METRIC_GROUP_PROPERTIES};
        ZE_CHECK(zetMetricGroupGetProperties(g, &gp));
        if (opt.group == gp.name) { group = g; break; }
    }
    if (!group) {
        fprintf(stderr, "ERROR: group '%s' not found on device %d (see --list)\n",
                opt.group.c_str(), opt.device);
        return 1;
    }
    auto metrics = get_metrics(group);

    if (opt.list_metrics) {
        for (auto& m : metrics)
            printf("%-40s %s\n", m.name.c_str(), m.description.c_str());
        return 0;
    }

    ze_context_handle_t ctx{};
    ze_context_desc_t cdesc{ZE_STRUCTURE_TYPE_CONTEXT_DESC};
    ZE_CHECK(zeContextCreate(driver, &cdesc, &ctx));
    ZE_CHECK(zetContextActivateMetricGroups(ctx, dev, 1, &group));

    zet_metric_streamer_desc_t sdesc{ZET_STRUCTURE_TYPE_METRIC_STREAMER_DESC};
    sdesc.notifyEveryNReports = 1024;
    sdesc.samplingPeriod = (uint32_t)(opt.period_ms * 1e6);  // ns
    zet_metric_streamer_handle_t streamer{};
    ze_result_t r = zetMetricStreamerOpen(ctx, dev, group, &sdesc, nullptr, &streamer);
    if (r != ZE_RESULT_SUCCESS) {
        fprintf(stderr, "FATAL: zetMetricStreamerOpen failed: 0x%x "
                "(another OA session active? paranoid sysctl?)\n", (unsigned)r);
        return 2;
    }
    fprintf(stderr, "sampling device %d group %s period %.1fms (actual %.1fms)\n",
            opt.device, opt.group.c_str(), opt.period_ms, sdesc.samplingPeriod / 1e6);

    FILE* out = stdout;
    if (!opt.out.empty()) {
        out = fopen(opt.out.c_str(), "w");
        if (!out) { perror("fopen --out"); return 1; }
    }
    if (!opt.json) {
        fprintf(out, "t_s");
        for (auto& m : metrics) fprintf(out, ",%s", m.name.c_str());
        fprintf(out, "\n");
    }

    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);

    const size_t nm = metrics.size();
    std::vector<double> sum(nm, 0.0), peak(nm, 0.0);
    uint64_t nreports = 0;
    auto t0 = std::chrono::steady_clock::now();
    std::vector<uint8_t> raw;
    std::vector<zet_typed_value_t> vals;

    while (!g_stop) {
        std::this_thread::sleep_for(std::chrono::milliseconds((long)opt.read_ms));
        double t = std::chrono::duration<double>(
                       std::chrono::steady_clock::now() - t0).count();
        if (opt.duration_s > 0 && t >= opt.duration_s) g_stop = true;

        size_t rawSize = 0;
        r = zetMetricStreamerReadData(streamer, UINT32_MAX, &rawSize, nullptr);
        if (r != ZE_RESULT_SUCCESS) {
            fprintf(stderr, "WARN: ReadData(size) failed: 0x%x\n", (unsigned)r);
            break;
        }
        if (rawSize == 0) continue;
        raw.resize(rawSize);
        r = zetMetricStreamerReadData(streamer, UINT32_MAX, &rawSize, raw.data());
        if (r != ZE_RESULT_SUCCESS) {
            fprintf(stderr, "WARN: ReadData failed: 0x%x\n", (unsigned)r);
            break;
        }

        uint32_t nvals = 0;
        r = zetMetricGroupCalculateMetricValues(
                group, ZET_METRIC_GROUP_CALCULATION_TYPE_METRIC_VALUES,
                rawSize, raw.data(), &nvals, nullptr);
        if (r != ZE_RESULT_SUCCESS || nvals == 0) continue;
        vals.resize(nvals);
        r = zetMetricGroupCalculateMetricValues(
                group, ZET_METRIC_GROUP_CALCULATION_TYPE_METRIC_VALUES,
                rawSize, raw.data(), &nvals, vals.data());
        if (r != ZE_RESULT_SUCCESS) continue;

        // nvals = nreports_in_batch * nm
        for (uint32_t off = 0; off + nm <= nvals; off += nm) {
            if (opt.json) fprintf(out, "{\"t\":%.3f", t);
            else          fprintf(out, "%.3f", t);
            for (size_t j = 0; j < nm; j++) {
                double v = value_as_double(vals[off + j]);
                if (opt.json) fprintf(out, ",\"%s\":%g", metrics[j].name.c_str(),
                                      std::isfinite(v) ? v : 0.0);
                else          fprintf(out, ",%g", v);
                if (std::isfinite(v)) {
                    sum[j] += v;
                    if (v > peak[j]) peak[j] = v;
                }
            }
            fprintf(out, opt.json ? "}\n" : "\n");
            nreports++;
        }
        fflush(out);
    }

    zetMetricStreamerClose(streamer);
    zetContextActivateMetricGroups(ctx, dev, 0, nullptr);
    zeContextDestroy(ctx);
    if (out != stdout) fclose(out);

    fprintf(stderr, "\n=== summary: %" PRIu64 " reports ===\n", nreports);
    if (nreports) {
        for (size_t j = 0; j < nm; j++) {
            // Highlight the metrics this tool exists for; print all anyway.
            fprintf(stderr, "%-36s avg %14.2f  peak %14.2f\n",
                    metrics[j].name.c_str(), sum[j] / (double)nreports, peak[j]);
        }
    }
    return 0;
}
