# Dockerfile — W3 沙箱基础镜像
# 构建：docker build -t ka-sandbox:py312-v1 .
FROM python:3.12-slim
ENV DEBIAN_FRONTEND=noninteractive
# 国内镜像源（构建加速；用户批准偏离计划默认官方源）
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null \
    || sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list
# 沙箱内环境：Git / curl / xz-utils（小包走 apt；node/npm 见下）
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl xz-utils \
    && rm -rf /var/lib/apt/lists/*
# Node.js + npm：8 连接并行分块下载官方 tar（本网络单连接限速 ~250KB/s 的 workaround，用户批准偏离）
COPY scripts/fetch_node.py /usr/local/bin/fetch_node.py
RUN python /usr/local/bin/fetch_node.py && \
    tar -xJf /node.tar.xz -C /usr/local --strip-components=1 && \
    rm /node.tar.xz && node --version && npm --version
# Python 测试框架与 schedule 时区测试依赖（清华 PyPI 镜像加速）
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple pytest pytz==2023.3
# 消除 Windows/Linux 文件权限位差异（W3 spec §5.1，容器内 git 行为与宿主一致）
RUN git config --system core.filemode false
WORKDIR /workspace
