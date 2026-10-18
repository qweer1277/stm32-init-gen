"""生成结果的数据结构。

生成器是纯函数 ``Stm32Config -> list[GeneratedFile]``：不写文件、
不依赖 GUI。写文件由调用方决定，这样 CLI、GUI、测试走同一条代码路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class GeneratedFile:
    """一个生成产物。"""

    # 相对输出目录的路径，如 "bsp_init.c"
    name: str
    content: str
    # 用途说明，供 CLI/GUI 展示
    description: str = ""
    # 对应的模板名，便于排查问题
    template: str = ""

    @property
    def lines(self) -> int:
        """行数，供摘要显示。"""
        return self.content.count("\n")

    @property
    def size_bytes(self) -> int:
        """UTF-8 字节数。"""
        return len(self.content.encode("utf-8"))

    def write(self, directory: str | Path, *, overwrite: bool = True) -> Path:
        """写入文件并返回落盘路径。

        ``overwrite=False`` 时若文件已存在则抛 ``FileExistsError``，
        供 GUI 做「是否覆盖」的确认。
        """
        target = Path(directory) / self.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and not overwrite:
            raise FileExistsError(f"文件已存在：{target}")
        # 统一用 \n 换行：C 编译器与 git 都能正确处理，
        # 且避免 Windows 下 CRLF 造成 diff 噪声
        target.write_text(self.content, encoding="utf-8", newline="\n")
        return target

    def summary(self) -> str:
        """一行摘要。"""
        text = f"{self.name}  ({self.lines} 行, {self.size_bytes} 字节)"
        if self.description:
            text += f"  — {self.description}"
        return text


@dataclass
class GenerateResult:
    """一次生成的完整结果。"""

    files: list[GeneratedFile] = field(default_factory=list)
    # 生成过程中的提示（例如"跳过了 XX 因为配置为空"）
    notes: list[str] = field(default_factory=list)
    # 生成器引用的全部标准库符号，供反编造护栏核对
    symbols_used: set[str] = field(default_factory=set)

    def add(self, item: GeneratedFile) -> None:
        self.files.append(item)

    def by_name(self, name: str) -> GeneratedFile | None:
        """按文件名查找产物。"""
        for item in self.files:
            if item.name == name:
                return item
        return None

    @property
    def total_lines(self) -> int:
        return sum(item.lines for item in self.files)

    def summary(self) -> str:
        """多行摘要。"""
        lines = [f"生成 {len(self.files)} 个文件，共 {self.total_lines} 行："]
        for item in self.files:
            lines.append(f"  {item.summary()}")
        if self.notes:
            lines.append("说明：")
            for note in self.notes:
                lines.append(f"  - {note}")
        return "\n".join(lines)

    def write_all(
        self,
        directory: str | Path,
        *,
        overwrite: bool = True,
        skip_existing: bool = False,
    ) -> tuple[list[Path], list[str]]:
        """把全部产物写入目录。

        返回 ``(已写入路径列表, 跳过说明列表)``。

        ``overwrite=False`` 与 ``skip_existing=True`` 的区别：

        - ``overwrite=False``：遇到已存在文件立即抛 ``FileExistsError``（全有或全无）
        - ``skip_existing=True``：跳过已存在文件，继续写其余的，并返回跳过说明

        CLI 的 ``--no-overwrite`` 用的是后者 —— 它的字面含义就是「跳过」，
        而「报错终止」会让用户逐个删文件才能继续。
        """
        written: list[Path] = []
        skipped: list[str] = []
        for item in self.files:
            target = Path(directory) / item.name
            if skip_existing and target.exists():
                skipped.append(f"{item.name}（已存在，跳过）")
                continue
            written.append(item.write(directory, overwrite=overwrite))
        return written, skipped
