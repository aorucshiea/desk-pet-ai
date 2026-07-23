---
name: systematic-debugging
description: "4 阶段系统化调试法：复现 → 缩小 → 根因 → 修复验证"
tags: [debugging, troubleshooting, root-cause]
version: 1.0.0
platforms: [windows, macos, linux]
---

# 系统化调试技能

## 4 阶段流程

### 阶段 1：完整复现
- 记录完整的错误信息（截图、日志、堆栈）
- 记录复现步骤和环境
- 确认问题不是偶发的

### 阶段 2：缩小范围
- 用二分法排除无关因素
  - 注释掉一半代码，看问题是否仍然存在
  - 如果存在 → 另一半有问题
  - 如果不存在 → 这一半有问题
- 检查是否输入数据的问题
- 检查是否环境差异（开发 vs 生产）

### 阶段 3：定位根因
- 列出所有可能的假设
- 对每个假设写一个最小验证
- **不要跳步**：验证完一个假设再进入下一个
- 检查常见的坑：
  - 边界条件（空值、0、负数）
  - 并发问题（竞态条件）
  - 资源泄漏（文件句柄、连接池）
  - 类型错误

### 阶段 4：修复验证
- 只改一个地方，然后测试
- 验证修复没有引入新问题
- 添加 regression test
- 提交时附上根因分析

## 工具建议

| 场景 | 工具 |
|------|------|
| Node.js | `--inspect` + Chrome DevTools |
| Python | `pdb` / `ipdb` / `breakpoint()` |
| 网络 | `curl -v` / `wireshark` / 浏览器 Network 面板 |
| 性能 | `perf` / `clinic.js` / `py-spy` |
