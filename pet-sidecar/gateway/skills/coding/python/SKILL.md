---
name: python-coding
description: "Python 编程最佳实践：类型注解、测试、错误处理、性能优化"
tags: [coding, python, best-practices]
version: 1.0.0
platforms: [windows, macos, linux]
action_type: generate
discipline: coding
carrier: any
---

# Python 编程技能

## 核心原则
1. **类型安全**：所有函数签名使用类型注解（type hints）
2. **测试优先**：关键逻辑先写测试再实现
3. **错误处理**：使用具体异常类型，避免裸 `except:`
4. **性能意识**：了解常见操作的复杂度（O(n) vs O(1)）

## 代码风格
- 遵循 PEP 8，行长度 100 字符
- 使用 `ruff` 进行 lint 和格式化
- 使用 `pathlib` 替代 `os.path`
- 使用 `dataclasses` 代替手写 `__init__`

## 常见模式

### 安全的文件读写
```python
from pathlib import Path

def read_config(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.warning("config load failed: %s", e)
        return None
```

### 异步超时保护
```python
import asyncio

async def fetch_with_timeout(url: str, timeout: float = 10.0) -> bytes | None:
    try:
        async with asyncio.timeout(timeout):
            return await fetch(url)
    except asyncio.TimeoutError:
        logger.error("request to %s timed out after %ss", url, timeout)
        return None
```
