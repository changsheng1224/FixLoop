"""单次 repair 成本模型单测：estimate_cost + pricing_table。"""

from agent_runtime.token_accounting import estimate_cost


class TestEstimateCost:
    def test_deepseek_default(self):
        """DeepSeek v4 pro 默认价格。"""
        report = {
            "input_tokens": 1_000_000,  # 1M
            "output_tokens": 0,
            "cache_read_tokens": 0,
        }
        cost = estimate_cost(report, model="deepseek-v4-pro")
        # input: 1M * 0.55 / 1M = 0.55
        assert cost == 0.55

    def test_with_cache_hit(self):
        """Cache hit 使用折扣价。"""
        report = {
            "input_tokens": 1_000_000,
            "output_tokens": 0,
            "cache_read_tokens": 500_000,
        }
        cost = estimate_cost(report, model="deepseek-v4-pro")
        # (1M - 0.5M)*0.55 + 0.5M*0.14 = 275000 + 70000 / 1M
        expected = round((500_000 * 0.55 + 500_000 * 0.14) / 1_000_000, 6)
        assert cost == expected

    def test_output_only(self):
        report = {"input_tokens": 0, "output_tokens": 1_000_000, "cache_read_tokens": 0}
        cost = estimate_cost(report, model="deepseek-v4-pro")
        assert cost == 2.19

    def test_unknown_model_zero(self):
        report = {"input_tokens": 1000, "output_tokens": 500}
        cost = estimate_cost(report, model="nonexistent-model")
        assert cost == 0.0

    def test_empty_report_zero(self):
        assert estimate_cost({}) == 0.0
