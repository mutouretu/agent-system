# JSON 过滤与去重任务

实现 `filter_records_json(records, min_score=0, limit=None)`，返回 JSON 字符串。

- records 是列表；其中非字典项忽略。
- 记录必须有非空字符串 id，以及实际存在的 score 字段。score 只允许 Python int，不能是 bool；缺失、None、字符串和浮点值均忽略。
- active 缺省为 True；只有 active is True 的记录有效。
- score >= min_score 才保留。min_score 必须是 int 且不是 bool，否则抛出 ValueError。
- 相同 id 保留最高分；最终按 score 降序、id 升序排列。
- limit 为 None 时不限制；否则必须是非负 int 且不是 bool。limit=0 必须返回空数组；非法 limit 抛出 ValueError。
- 只输出 id 和 score；JSON 使用 UTF-8 字符、紧凑分隔符和排序后的对象键。
- 不修改输入，不改变测试，不增加依赖。

反馈协议：每次运行返回完整 stdout、stderr、pytest 长 traceback、JUnit 和结构化统计。
初始运行不计入重试；最多允许 3 次基于最新反馈的代码修订。一次修订可以修复多个原因，无需人为拆分。
