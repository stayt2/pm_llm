#!/bin/bash

# 安装vllm库
pip install vllm

# 指定运行程序的GPU设备编号为3，选择使用编号为3的GPU,与tp对应
export CUDA_VISIBLE_DEVICES=3

# 启动VLLM API服务器，以下为相关参数说明：
# - --model: 指定要加载的模型的路径
# - --served-model-name: 模型服务的名称
# - --dtype: 模型数据类型自动选择
# - --port: 指定API服务器监听的端口号
# - --host: 指定API服务器监听的网络地址，0.0.0.0允许所有IP地址访问
# - --gpu-memory-utilization: 限制GPU内存使用上限为90%
# - --max-model-len: 指定模型支持的最大输入长度（token数量）
# - -tp: 张量并行度，1表示关闭张量并行，即由单个GPU处理所有张量运算

python -m vllm.entrypoints.openai.api_server \
    --model medical-model \
    --served-model-name doctor \
    --dtype=auto \
    --port 8024 \
    --host 0.0.0.0 \
    --gpu-memory-utilization 0.9 \
    --max-model-len 512 \
    -tp 1
