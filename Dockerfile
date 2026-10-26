FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app
COPY requirements.txt .
# 默认走清华源，国内构建快很多；想换源在构建时传 --build-arg PIP_INDEX=...
ARG PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
RUN pip install --no-cache-dir --timeout 60 -i ${PIP_INDEX} -r requirements.txt

COPY app ./app
COPY persona ./persona

RUN mkdir -p /data
VOLUME ["/data"]

CMD ["python", "-m", "app.main"]
