# 闭环供水管网平衡台 —— 单镜像部署
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖以利用 Docker 层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码（.dockerignore 已排除测试与虚拟环境）
COPY app ./app

EXPOSE 8000

# 容器内直接监听 0.0.0.0:8000，由 Compose/宿主映射端口
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
