# probes — 「先验再说」用的一次性探针

这些脚本存在的唯一理由: **让"去验一下"这件事便宜到没有借口不做。**

2026-08-23 一天里, 有五个结论是被这类探针推翻的 —— 每一个当时都"听起来完全合理":

| 我以为 | 探针给出的事实 |
|---|---|
| SDK 没把 thinking 交出来 | 交了, 但**文本是空的** (只有 signature) |
| sonnet 不产生 thinking 块 | 产生, high/medium 都产生 |
| 探不到 active 帧 = 功能没生效 | 我自己的进程还没铺到新代码 |
| `ResultError` 能让判据读类型 | 它走不到常驻 streaming 这条路, 错误以 ResultMessage 到达 |
| 0.2.128 没有 api_error_status 等字段 | **本来就有, 而且带值** (我照 changelog 推的, 没验) |

共同形状: **未验的解释被放在了结论的位置上**, 而验它只要一条命令。

## 用法

```bash
venv/bin/python probes/thinking_events.py   # 思考块的事件序 (start/delta/stop) 与文本长度
venv/bin/python probes/api_error.py         # 造一次真 429, 看 ResultMessage 上有哪些字段有值
```

两个都**只跑一轮、不碰任何 bot**, 走各自的 venv —— 想比较 SDK 版本就换解释器:

```bash
~/.fleet-claude-bridge/venv-next/bin/python probes/api_error.py   # 0.2.144
```

`api_error.py` 起一个本地假上游 (一律回 429) 并把 `ANTHROPIC_BASE_URL` 指过去, 所以
它造的是**上游返回失败状态**这条路, 与真实 429/500/529 同形; 传输层断连是另一类,
不走 ResultMessage。

## 加新探针的判据

只加**用来证伪某个断言**的脚本, 而且在文件头写清楚: **它要回答哪一句话**。
不要变成第二套测试 —— 测试钉住"代码按我想的算", 探针回答"我想的对不对"。
