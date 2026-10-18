# 测试

```powershell
python -m pytest tests\ -q
python -m pytest tests\ -q -v
```

| 文件 | 覆盖内容 |
|---|---|
| `test_symbols.py` | 符号索引：数值精确性、枚举成员、条件编译、函数签名、结构体成员、反编造抛异常 |
| `test_tools.py` | 提取器与反编造护栏本身（文本预处理、表达式求值、条件编译、误报防护） |

## 测试设计原则

**期望值必须来自对标准库源码的实地核对，不能从文档或记忆里抄。**
例如 `GPIO_Mode_AF_PP == 0x18` 这条断言，依据是
`Library\stm32f10x_gpio.h:75`，不是数据手册。
（库源码目录由环境变量 `STM32_STDLIB_ROOT` 指定；未设置时依赖它的
用例会自动 skip，见 `tests/conftest.py`。）

几个专门守着易错点的回归测试：

- `test_enum_values` —— 覆盖 `GPIO_Speed_*` 的**递增省略**写法，
  以及 `ENABLE = !DISABLE` 的 **C 逻辑非**（Python 里 `!` 是语法错误）
- `test_irqn_only_medium_density` —— 断言 `OTG_FS_IRQn` **不存在**，
  它属于互联型（CL），混进来说明条件编译求值失效
- `test_struct_members_have_no_comment_text` —— 断言成员名都是合法标识符，
  防止多行注释的文字被误当成员名
- `test_guard_accepts_valid_code` —— 合法代码零误报。
  误报会让人绕过护栏，比没有护栏更糟
- `test_resolve_macro_values_chain` —— 宏的依赖链解析
  （`GPIOA_BASE` → `APB2PERIPH_BASE` → `PERIPH_BASE`）

## 新增测试时

- 涉及标准库符号的，先 `grep` 头文件确认实际值，再写断言，并注明文件与行号
- 涉及生成代码的，用 `check_no_invented.py` 过一遍
- 不要写「只要不抛异常就算过」的弱断言
