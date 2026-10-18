# 开发期工具

- `extract_symbols.py` —— 扫描标准库头文件，产出 `src/stm32gen/stdlib_ref/symbols.json`
- `check_no_invented.py` —— 校验生成的 C 代码里每个 STM32 符号都真实存在于标准库

两者是「禁止编造函数/宏/枚举」这条硬性约束的执行者。

```powershell
python tools\extract_symbols.py
python tools\check_no_invented.py out\
python tools\check_no_invented.py --selftest
```

## extract_symbols.py

解析 `Start/` `Library/` `User/` 下 28 个头文件，抽取宏、枚举值、函数原型、类型定义。
按 `STM32F10X_MD` + `USE_STDPERIPH_DRIVER` 做条件编译求值，只保留中容量分支。

脚本末尾带 58 项关键符号自检（含数值比对），任一不符即以退出码 2 失败。

### 五个必须注意的解析细节

1. **注释先整体抹掉再按行解析**。结构体成员普遍带多行注释：

   ```c
   uint16_t GPIO_Pin;   /*!< 指定要配置的 GPIO 引脚。
                             该参数可以取 @ref GPIO_pins_define 中的任意值 */
   ```

   逐行删注释时首行没有 `*/`，`GPIO 引脚。` 会被当成成员名。
   正确做法是把注释内容替换成空格并**保留换行**（行号不变）。

2. **`typedef enum` 与 `{` 通常不同行**。只匹配 `typedef enum\s*\{`
   会漏掉全部枚举与结构体。

3. **枚举值有递增省略与 C 运算符**：
   - `GPIO_Speed_10MHz = 1, GPIO_Speed_2MHz, GPIO_Speed_50MHz`（省略写法）
   - `ENABLE = !DISABLE`（C 逻辑非，Python 里 `!` 是语法错误，必须转换）

4. **一个 typedef 可带多个别名**：`} FlagStatus, ITStatus;`
   只取第一个会漏掉 `ITStatus`。

5. **禁用回溯型正则**。`(?:[A-Za-z_]\w*\s+|\*\s*)+?` 这类嵌套量词在长行上
   会灾难性回溯，实测直接把提取挂死。函数原型改为手工定位括号。

同理，`resolve_macro_values` 避开「每个已知宏做一次 `re.sub`」的 O(n²) 写法，
改用单次标识符扫描 + 回调查表。

## check_no_invented.py

扫描 C 代码中所有标准库前缀
（`GPIO_` / `USART_` / `NVIC_` / `RCC_` / `FLASH_` / `AFIO_` / `IS_` …）
的标识符，逐一比对符号表。

> `FLASH_` 前缀是加 RCC/Flash 初始化代码时补上的：在此之前生成代码从不碰
> Flash 库，护栏不查它也没人会发现。现在 `bsp_rcc.c` 会调用 `FLASH_SetLatency`
> / `FLASH_PrefetchBufferCmd`，前缀漏掉就等于这两条调用不受护栏保护。
> **加新外设时先确认 `LIB_PREFIXES` 覆盖了它的前缀** —— 这是最容易漏的一步。

两条硬性要求，都有测试守着：

- **不能漏检** —— 编造符号必须被抓到
- **不能误报** —— 误报会让人绕过护栏，比没有护栏更糟

已处理的三个误报来源：

| 来源 | 例子 | 处理 |
|---|---|---|
| 结构体成员访问 | `cfg.GPIO_Pin`、`p->USART_BaudRate` | 排除 `.` / `->` 之后的标识符 |
| 注释与字符串 | `// GPIO_Fake`、`"USART_Nope"` | 预处理时替换为空格 |
| 非库前缀标识符 | `my_own_function()` | 只检查 `LIB_PREFIXES` |

需要引用库外符号时，在**该行**写 `/* noqa: invented-symbol */` 显式豁免。
"""
