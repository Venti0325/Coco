# Coco

基于终端的轻量化AI编程助手。

## 快速开始

```bash
# 创建虚拟环境并以可编辑模式安装
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# 复制并编辑环境配置
cp .env.example .env

# 运行
coco
```

## 项目结构

```
src/core/
  main.py      – CLI 入口
  config.py    – 分层配置加载（TOML / 环境变量 / CLI）
  llm.py       – 统一 LLM 客户端（Anthropic / OpenAI 双后端）
  paths.py     – 集中路径约定
  models.py    – 共享数据模型与类型
  log.py       – 控制台输出助手
  tools/       – 内置工具实现
tests/         – 单元测试与集成测试
```
