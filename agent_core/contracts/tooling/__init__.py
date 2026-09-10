"""Contract tooling：Schema 生成、Payload 校验与镜像传输。

本子包只在 **Wiki canonical 仓** 用于撰写与验证契约载荷；Coding 仓只承载由
受控 bundle 提升得到的镜像，不运行其中的生成逻辑（生成能力随镜像一起同步，
以保证消费端能独立复算并验证）。
"""
