# syntax=docker/dockerfile:1
# The same image ships as the Home Assistant add-on and as a plain container. Two stages: build
# a wheel, then install only the wheel into a slim runtime as an unprivileged user.

FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN python -m pip install --no-cache-dir --upgrade pip build \
    && python -m build --wheel --outdir /dist

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 pascl
COPY --from=build /dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl
USER pascl
WORKDIR /home/pascl
ENTRYPOINT ["pascl"]
CMD ["--help"]
