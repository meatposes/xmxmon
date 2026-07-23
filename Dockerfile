FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
        software-properties-common ca-certificates gnupg && \
    add-apt-repository -y ppa:kobuk-team/intel-graphics && \
    apt-get update && apt-get install -y --no-install-recommends \
        g++ libze-dev libze1 libze-intel-gpu1 \
        intel-metrics-discovery intel-metrics-library \
        python3 python3-yaml && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY xmxmon.cpp xmx-summary.py check-driver-updates.sh xmxmond.py \
     xmxmon-tui.py wui.html ./
# Fallback config so an unconfigured container still binds somewhere reachable.
COPY xmxmon.yaml.example /app/xmxmon.yaml.default
RUN g++ -O2 -o xmxmon xmxmon.cpp -lze_loader && \
    chmod +x xmx-summary.py check-driver-updates.sh xmxmond.py xmxmon-tui.py

ENV ZET_ENABLE_METRICS=1
# Unbuffered, or startup warnings never reach `docker logs`.
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["/app/xmxmon"]
