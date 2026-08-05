$skill_hint_block
根据 Issue 和嫌疑文件搜索相关代码：
$issue

嫌疑文件: $suspect_files
请用 find_test / grep / search / read_file 收集上下文，最后调用 submit_retrieved_context 提交（related_tests 必填非空）。
不要输出散文 JSON、`<final>` 或 Markdown；以工具提交结束。
