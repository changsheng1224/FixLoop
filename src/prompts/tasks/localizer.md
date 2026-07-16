$skill_hint_block
定位以下问题：
$issue

$suspect_files_line

$issue_type_hints

不要调用工具，不要输出 <function_calls>、<tool>、<final> 或 Markdown。
只输出合法 JSON 数组，每个元素使用以下字段：
file_path, start_line, end_line, function_name, reason, confidence。

示例：
[{"file_path":"calculator.py","start_line":6,"end_line":6,"function_name":"add","reason":"异常信息指向该位置","confidence":0.9}]
