FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --upgrade pip==26.3 setuptools==84.2.1 \
    && python -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.15.0 \
    && python -m pip install --no-cache-dir laya==0.4.35 transformers==5.23.0 paho-mqtt==2.1.0 \
    && python -m pip check

COPY ai4i_trial.py ai4i_evaluation.py ai4i_simulation.py ai4i_replay_cases.py gateway_mqtt.py mqtt_bench.py ./
CMD ["python", "gateway_mqtt.py"]
