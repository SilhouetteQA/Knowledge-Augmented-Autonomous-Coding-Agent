# Coding baseline — w11a-v1

- 源运行目录（未入库，`/output/` 被忽略）：`output\benchmark\20261012-183750`
- 收集时间：2026-10-12T18:38:36
- harness: `723c4a524348670c231109ce23c72745aa659134` (feature/pre-extraction-baseline)
- 结果：2/5（raw 40% / adjusted 50%）
- fixture pin 不匹配：无

## 复算校验

```powershell
Get-Content SHA256SUMS | ForEach-Object { $h,$f = $_ -split '  ',2;
  if ((Get-FileHash $f -Algorithm SHA256).Hash.ToLower() -ne $h) { "MISMATCH $f" } }
```

## 对应 Spec

`docs/specs/2026-10-12-pre-extraction-benchmark-freeze.md` §3.1（Coding）/ §3.4（产物）/ §3.5（容差）
