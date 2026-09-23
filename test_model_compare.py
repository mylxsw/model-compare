import io
import unittest
from unittest.mock import patch

import model_compare as mc


class StreamTests(unittest.TestCase):
    def profile(self, protocol):
        return {"protocol": protocol, "base_url": "https://example.test/v1",
                "models": {"main": "test"}}

    def test_openai_stream(self):
        stream = io.BytesIO(
            b'data: {"type":"response.output_text.delta","delta":"Hello"}\n\n'
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":3,"output_tokens":1,"output_tokens_details":{"reasoning_tokens":1}}}}\n\n'
            b'data: [DONE]\n\n'
        )
        with patch.object(mc.urllib.request, "urlopen", return_value=stream):
            result = mc.run_one("openai", "main", self.profile("openai-responses"),
                                1, "fake", "prompt", "", 100, 2)
        self.assertEqual((result["status"], result["text"]), ("ok", "Hello"))
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (3, 1))
        self.assertEqual(result["thinking_tokens"], 1)
        self.assertIsNotNone(result["ttft_ms"])

    def test_anthropic_stream(self):
        stream = io.BytesIO(
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":7}}}\n\n'
            b'data: {"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"hidden"}}\n\n'
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"Answer"}}\n\n'
            b'data: {"type":"message_delta","usage":{"output_tokens":2}}\n\n'
        )
        with patch.object(mc.urllib.request, "urlopen", return_value=stream):
            result = mc.run_one("anthropic", "main", self.profile("anthropic-messages"),
                                1, "fake", "prompt", "", 100, 2)
        self.assertEqual((result["text"], result["input_tokens"], result["output_tokens"]),
                         ("Answer", 7, 2))

    def test_gemini_stream_and_report(self):
        stream = io.BytesIO(
            b'data: {"candidates":[{"content":{"parts":[{"text":"Hi "}]}}]}\n\n'
            b'data: {"candidates":[{"content":{"parts":[{"text":"there"}]}}],'
            b'"usageMetadata":{"promptTokenCount":4,"candidatesTokenCount":2,"thoughtsTokenCount":1}}\n\n'
        )
        with patch.object(mc.urllib.request, "urlopen", return_value=stream):
            result = mc.run_one("gemini", "main", self.profile("gemini-generate-content"),
                                1, "fake", "prompt", "", 100, 2)
        self.assertEqual(result["text"], "Hi there")
        self.assertEqual(result["thinking_tokens"], 1)
        report = mc.report_markdown({"created_at": "now", "models": [
            {"provider": "gemini", "alias": "main", "model": "test",
             "protocol": "gemini-generate-content", "thinking": {"mode": "default"}}],
                                     "results": [result]})
        self.assertIn("Hi there", report)
        self.assertIn("1/1", report)

    def test_openai_compatible_gateway(self):
        profile = self.profile("openai-chat")
        request = mc.make_request(profile, "test", "secret", "question", "be brief", 50)
        self.assertEqual(request.full_url, "https://example.test/v1/chat/completions")
        self.assertIn("Bearer secret", request.get_header("Authorization"))
        self.assertEqual(mc.json.loads(request.data)["messages"][0]["role"], "system")
        stream = io.BytesIO(
            b'data: {"choices":[{"delta":{"content":"Gateway answer"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
            b'data: [DONE]\n\n'
        )
        with patch.object(mc.urllib.request, "urlopen", return_value=stream):
            result = mc.run_one("gateway", "main", profile, 1, "fake", "question", "", 50, 2)
        self.assertEqual((result["text"], result["input_tokens"], result["output_tokens"]),
                         ("Gateway answer", 5, 2))

    def test_example_configuration(self):
        profiles = mc.load_config("model-compare.example.json")
        self.assertEqual(profiles["my_gateway"]["models"]["fast"], "provider/model-id")
        self.assertEqual(mc.model_config(profiles["gemini"]["models"]["fast_budget"])["thinking"],
                         {"mode": "on", "budget_tokens": 4096})
        self.assertEqual(mc.api_key_for({"api_key": "private"}), "private")

    def test_thinking_protocol_mappings(self):
        cases = [
            ("openai-responses", "gpt-5", {"mode": "on", "effort": "high"},
             ["reasoning", "effort"], "high"),
            ("openai-responses", "gpt-5", {"mode": "off"},
             ["reasoning", "effort"], "none"),
            ("openai-chat", "proxy-model", {"mode": "on", "effort": "low"},
             ["reasoning_effort"], "low"),
            ("anthropic-messages", "claude-sonnet-4-6", {"mode": "on", "effort": "medium"},
             ["thinking", "type"], "adaptive"),
            ("anthropic-messages", "claude-sonnet-4-5", {"mode": "on", "budget_tokens": 2048},
             ["thinking", "budget_tokens"], 2048),
            ("anthropic-messages", "claude-sonnet-5", {"mode": "off"},
             ["thinking", "type"], "disabled"),
            ("gemini-generate-content", "gemini-2.5-flash", {"mode": "off"},
             ["generationConfig", "thinkingConfig", "thinkingBudget"], 0),
            ("gemini-generate-content", "gemini-2.5-flash", {"mode": "on", "budget_tokens": 4096},
             ["generationConfig", "thinkingConfig", "thinkingBudget"], 4096),
            ("gemini-generate-content", "gemini-3.5-flash", {"mode": "on", "effort": "low"},
             ["generationConfig", "thinkingConfig", "thinkingLevel"], "LOW"),
        ]
        for protocol, model, thinking, path, expected in cases:
            with self.subTest(protocol=protocol, model=model, thinking=thinking):
                request = mc.make_request(self.profile(protocol), model, "fake", "prompt", "", 8192,
                                          thinking)
                value = mc.json.loads(request.data)
                for key in path:
                    value = value[key]
                self.assertEqual(value, expected)

    def test_incompatible_thinking_fails_before_request(self):
        cases = [
            ("openai-chat", "proxy", {"mode": "on", "budget_tokens": 2048}),
            ("anthropic-messages", "claude-sonnet-4-5", {"mode": "on", "budget_tokens": 2048}),
            ("gemini-generate-content", "gemini-2.5-pro", {"mode": "off"}),
            ("gemini-generate-content", "gemini-2.5-flash", {"mode": "on", "effort": "high"}),
            ("gemini-generate-content", "gemini-3.5-flash", {"mode": "on", "budget_tokens": 2048}),
        ]
        for protocol, model, thinking in cases:
            with self.subTest(protocol=protocol, model=model, thinking=thinking):
                with self.assertRaises(ValueError):
                    mc.make_request(self.profile(protocol), model, "fake", "prompt", "", 1024,
                                    thinking)
        for protocol, model, thinking in [
            ("openai-responses", "gpt-6-astra", {"mode": "off"}),
            ("anthropic-messages", "claude-sonnet-4-5", {"mode": "on"}),
            ("anthropic-messages", "claude-opus-5", {"mode": "on", "budget_tokens": 2048}),
            ("gemini-generate-content", "gemini-3.5-flash", {"mode": "off"}),
        ]:
            with self.subTest(protocol=protocol, model=model, thinking=thinking):
                with self.assertRaises(ValueError):
                    mc.make_request(self.profile(protocol), model, "fake", "prompt", "", 8192,
                                    thinking)
        with self.assertRaises(ValueError):
            mc.model_config({"id": "x", "thinking": {"mode": "off", "effort": "high"}})

    def test_chat_completion_token_field_option(self):
        profile = self.profile("openai-chat")
        profile["chat_max_tokens_field"] = "max_completion_tokens"
        request = mc.make_request(profile, "reasoning-model", "fake", "prompt", "", 2048,
                                  {"mode": "on", "effort": "medium"})
        body = mc.json.loads(request.data)
        self.assertEqual(body["max_completion_tokens"], 2048)
        self.assertNotIn("max_tokens", body)


if __name__ == "__main__":
    unittest.main()
