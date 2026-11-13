FROM python:3.11-slim-bookworm@sha256:90d117a7f1ef6535dc75831e1a9184725757fede0d4d424d7977fb5429d50610 AS native-builder
COPY build-dependencies.lock /src/build-dependencies.lock
RUN apt-get update && xargs apt-get install -y --no-install-recommends < /src/build-dependencies.lock \
    && rm -rf /var/lib/apt/lists/*
COPY native/exact.cpp /src/exact.cpp
RUN mkdir /out && g++-12 -std=c++17 -O3 -fno-fast-math -ffp-contract=off \
    -fPIC -shared -fopenmp -pthread -static-libstdc++ -static-libgcc \
    /src/exact.cpp -o /out/_native.so

FROM python:3.11-slim-bookworm@sha256:90d117a7f1ef6535dc75831e1a9184725757fede0d4d424d7977fb5429d50610

WORKDIR /app
COPY minivectordb /app/minivectordb
COPY --from=native-builder /out/_native.so /app/minivectordb/_native.so
COPY --from=native-builder /usr/lib/x86_64-linux-gnu/libgomp.so.1.0.0 /usr/lib/x86_64-linux-gnu/libgomp.so.1.0.0
RUN ln -s libgomp.so.1.0.0 /usr/lib/x86_64-linux-gnu/libgomp.so.1
# Compile all submitted Python source during the image build; no third-party
# runtime packages, network access, or prebuilt local binaries are required.
RUN python -m compileall -q /app/minivectordb && mkdir -p /data
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
VOLUME ["/data"]
ENTRYPOINT ["python", "-m", "minivectordb", "--wire", "jsonl"]
CMD ["--data-dir", "/data"]
