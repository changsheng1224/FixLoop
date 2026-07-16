$skill_hint_block
根据 Issue 和嫌疑文件搜索相关代码：
$issue

嫌疑文件: $suspect_files
请用 find_test 和 search 收集上下文，直接输出 RetrievedContext JSON。
只输出一个合法 JSON 对象；不要输出 <final>、Markdown、解释或前后缀文本。
一次尝试失败后编排层会降级到规则检索，不要自行请求重试。
