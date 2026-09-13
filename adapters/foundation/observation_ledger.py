"""client-local component provenance sidecar（规范术语）。

母 Spec §9.1 明确了它的边界：

```text
Coding component provenance
= project-local, in-memory, client-scoped observation support

It is NOT:
- a business cost source of truth
- a durable accounting system
- a shared contract
- an Evidence deduplication mechanism
```

因此本模块：

- **不落盘**、不跨进程、不跨仓；没有磁盘格式，也没有恢复逻辑
- **client-scoped**：每个 `OpenAICompatClient` 实例一份，不是全局单例
- **不设计**新的全局 invocation ID，也不做 Evidence 去重
- **不改** `tokens_total`：它只是观察支持的旁路记录
- `off` 模式下由 runtime 拒绝分配（见 :mod:`adapters.foundation.runtime`）

游标语义（Spec 06 step 6 / step 8）：

```text
start = ledger.cursor()                        case 开始
... 期间的调用 append 组成项 ...
end   = ledger.cursor()                        case 结束
components = ledger.range_slice(start, end)    本 case 的边界稳定切片

# 便捷形式（case 结束时立即消费）：
mark = ledger.cursor(); ...; components = ledger.slice(mark)
```

- `range_slice(start, end)` 两端都在取切片前确定，之后的新追加不会混入
- `slice(mark)` 等价于 `range_slice(mark, cursor())`，取的是调用时刻的窗口；
  因此一个 mark 应在该 case 结束时就地消费，不要跨到下个 case 之后再读
- 两种切片都只按游标切分，不做成功/失败过滤 —— 因此「根本没有调用」与「调用后失败」
  会得到不同的组成项集合，进而产生不同的 CostSummary 语义
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Final

from adapters.foundation.facts import CodingLegacyCostFacts, CodingLegacyUsageFacts


@dataclass(frozen=True)
class ComponentRecord:
    """一次被观察到的调用所产生的最小组成项。"""

    usage_facts: CodingLegacyUsageFacts
    cost_facts: CodingLegacyCostFacts
    model: str | None = None
    #: 既有安全稳定 call ID（若调用点本来就有）；不存在时为 ``None``，不新建 ID。
    call_id: str | None = None


class ComponentLedger:
    """client-local、内存内的组成项账本。

    :param max_components: 上限；超出时丢弃最旧的组成项，防止长时间运行无界增长。
    """

    #: 默认容量上限（远超任何单个 case 的调用次数）。
    DEFAULT_MAX_COMPONENTS: Final[int] = 10_000

    def __init__(self, *, max_components: int = DEFAULT_MAX_COMPONENTS) -> None:
        if max_components <= 0:
            raise ValueError("max_components 必须为正数")
        self._components: list[ComponentRecord] = []
        self._max = max_components
        self._lock = threading.Lock()

    # -- 写入 ------------------------------------------------------------- #

    def append(self, record: ComponentRecord) -> None:
        """追加一个组成项。"""
        with self._lock:
            self._components.append(record)
            overflow = len(self._components) - self._max
            if overflow > 0:
                del self._components[:overflow]

    # -- 游标 ------------------------------------------------------------- #

    def cursor(self) -> int:
        """返回当前游标（已记录组成项数）；case 开始时取 start，结束时取 end。"""
        with self._lock:
            return len(self._components)

    def range_slice(self, start: int, end: int) -> tuple[ComponentRecord, ...]:
        """返回 ``[start, end)`` 区间内的组成项（**边界稳定**）。

        两端都在调用前确定，之后的新追加不会混入 —— 这是 case 级切片的推荐用法。
        """
        self._check_mark(start, "start")
        self._check_mark(end, "end")
        if end < start:
            raise ValueError(f"end({end}) 不能小于 start({start})")
        with self._lock:
            return tuple(self._components[start:end])

    def slice(self, mark: int) -> tuple[ComponentRecord, ...]:
        """返回 ``mark`` 到**调用时刻**末尾的组成项。

        等价于 ``range_slice(mark, cursor())``。只做游标切分，不区分成功与失败。
        """
        self._check_mark(mark, "mark")
        with self._lock:
            return tuple(self._components[mark:])

    @staticmethod
    def _check_mark(mark: object, name: str) -> None:
        if not isinstance(mark, int) or isinstance(mark, bool) or mark < 0:
            raise ValueError(f"{name} 必须是非负整数，实际为 {mark!r}")

    def cost_facts_slice(self, mark: int) -> tuple[CodingLegacyCostFacts, ...]:
        """只取组成项的 cost facts（供 summary 映射使用）。"""
        return tuple(record.cost_facts for record in self.slice(mark))

    # -- 只读属性 --------------------------------------------------------- #

    @property
    def component_count(self) -> int:
        with self._lock:
            return len(self._components)

    def __len__(self) -> int:
        return self.component_count


__all__ = ["ComponentRecord", "ComponentLedger"]
