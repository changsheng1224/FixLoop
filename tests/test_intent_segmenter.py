"""Tests for intent text segmenter."""

from agent_runtime.intent.segmenter import segment


class TestSegmenter:
    def test_single_sentence(self):
        segs = segment("帮我看看这个函数做什么？")
        assert len(segs) == 1
        assert "函数" in segs[0].text

    def test_chinese_sentence_split(self):
        segs = segment("帮我修这个 TypeError。只用改 foo.py")
        assert len(segs) >= 2
        assert "TypeError" in segs[0].text or "修" in segs[0].text
        assert "foo.py" in segs[1].text or "只用" in segs[1].text

    def test_sequential_cue(self):
        segs = segment("请记住用 pytest。然后帮我修这个失败。")
        assert len(segs) >= 2
        assert any(s.cue == "sequential" for s in segs)

    def test_additive_cue(self):
        segs = segment("这个函数是干什么的？另外 AgentLoop 呢？")
        assert len(segs) >= 2
        assert any(s.cue == "additive" for s in segs)

    def test_protect_file_extension(self):
        segs = segment("请打开 config.py 并解释。")
        # should not split on the dot in config.py into nonsense
        assert all("config" in s.text or "解释" in s.text or "打开" in s.text for s in segs)
        joined = " ".join(s.text for s in segs)
        assert "config.py" in joined

    def test_short_fragment_merge(self):
        segs = segment("修好它。嗯")
        # 「嗯」 < 2 chars → merge into previous
        assert len(segs) == 1
        assert "嗯" in segs[0].text

    def test_traceback_stays_one_block(self):
        text = (
            'Traceback (most recent call last):\n'
            '  File "app.py", line 1, in <module>\n'
            "TypeError: bad"
        )
        segs = segment(text)
        assert len(segs) == 1
        assert "TypeError" in segs[0].text
        assert "Traceback" in segs[0].text

    def test_blank_line_blocks(self):
        segs = segment("记住用 pytest。\n\n然后修这个失败。")
        assert len(segs) >= 2
