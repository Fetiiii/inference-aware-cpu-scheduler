# C04A lightweight counter preflight

## Host and constraints

- Inspection date: 2026-09-06.
- Current host: Intel Core i7-14650HX.
- Current kernel: `7.2.2-1-cachyos`.
- Historical C01/C02 artifacts record kernel `7.2.0-1-cachyos`; the current
  preflight must not be mistaken for a counter result from those runs.
- No package was installed, no privilege was requested, and no kernel,
  governor, EPP, or power setting was changed.

## Read-only checks

```text
$ perf --version
zsh: command not found: perf

$ cat /proc/sys/kernel/perf_event_paranoid
2

$ zgrep -E 'CONFIG_(HW_)?PERF_EVENTS' /proc/config.gz
CONFIG_PERF_EVENTS=y
CONFIG_PERF_EVENTS_INTEL_UNCORE=m
CONFIG_PERF_EVENTS_INTEL_RAPL=m
CONFIG_PERF_EVENTS_INTEL_CSTATE=m
...

$ command -v perf
(no result)

$ find /usr/bin /usr/local/bin -maxdepth 1 -type f -name 'perf*'
/usr/bin/perfquery
```

`perfquery` is an InfiniBand utility and is not Linux `perf`.

The planned trivial-process probe was:

```text
perf stat -e cycles,instructions,cache-misses,context-switches,cpu-migrations -- true
```

It could not start because the `perf` frontend is absent. Therefore supported
event enumeration and actual unprivileged `perf_event_open` access were not
tested; `perf_event_paranoid=2` alone is not treated as proof that a given
event would succeed.

## Availability decision

**Usable unprivileged perf counters now: NO.** The kernel has perf-events
support, but the required userspace frontend is missing. C04A does not install
it automatically and does not propose a privileged workaround.

| candidate | present C04A status | non-privileged source |
|---|---|---|
| cycles | unavailable / not probeable | would require a successful `perf stat` probe |
| instructions | unavailable / not probeable | would require a successful `perf stat` probe |
| IPC | unavailable / not derivable | needs usable cycles and instructions |
| cache misses | unavailable / not probeable | would require a successful `perf stat` probe |
| context switches | available | existing per-TID `/proc/<pid>/task/<tid>/sched` snapshots |
| CPU migrations | available | existing per-TID `/proc/<pid>/task/<tid>/sched` snapshots |
| frequency summary | available | existing readable cpufreq sysfs sampler |
| package temperature | available | existing readable hwmon/coretemp path |

The existing harness's context-switch and migration values are runtime
scheduler totals, not hardware-counter substitutes for cycles, instructions,
IPC, or cache misses.

If a later diagnostic is authorized after `perf` is installed by the user,
the same trivial-process probe must be repeated before attaching counters to
any inference process. C04A supplies no benchmark command.
