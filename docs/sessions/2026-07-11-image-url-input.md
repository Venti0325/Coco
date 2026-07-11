# 远程图片 URL 输入

## 目标

让 Coco 的统一图片内容块支持远程 URL，并在 OpenAI-compatible provider 适配层保留原始签名 URL。

## 计划

1. 扩展 OpenAI-compatible 用户内容块转换，识别 Anthropic 风格的 URL image source。
2. 保留现有 base64 image source 行为。
3. 增加 URL 与 base64 的回归测试并运行完整测试集。

## 不做的事

- 不下载远程图片。
- 不把 URL 转换成 base64。
- 不替调用方判断具体模型是否支持视觉输入。

## 验证

- `pytest tests/test_llm.py -v`
- `pytest tests/ -v`

## Summary

- `src/core/llm.py` 现在把 `source.type == "url"` 的图片转换为 OpenAI-compatible `image_url`，不复制图片字节。
- Anthropic provider 继续直接使用相同的内部 URL source；base64 输入保持兼容。
- 新增签名 URL 保真回归测试。
