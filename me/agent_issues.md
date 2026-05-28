# Agent issues inbox

bot 在 dream cron 通过 `write_agent_issue` 工具自主写入的 issue。**只追加，不改写**——
admin 在 webUI「Agent 自治」tab 浏览。

格式：

```
## YYYY-MM-DDTHH:MM [category] 标题
**user_id**: <or "global">
**severity**: low | medium | high
**body**:
正文...
---
```

最新的在文件末尾。

---
