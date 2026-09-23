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
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":3,"output_tokens":1}}}\n\n'
            b'data: [DONE]\n\n'
        )
        with patch.object(mc.urllib.request, "urlopen", return_value=stream):
            result = mc.run_one("openai", "main", self.profile("openai-responses"),
                                1, "fake", "prompt", "", 100, 2)
        self.assertEqual((result["status"], result["text"]), ("ok", "Hello"))
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (3, 1))
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
            b'"usageMetadata":{"promptTokenCount":4,"candidatesTokenCount":2}}\n\n'
        )
        with patch.object(mc.urllib.request, "urlopen", return_value=stream):
            result = mc.run_one("gemini", "main", self.profile("gemini-generate-content"),
                                1, "fake", "prompt", "", 100, 2)
        self.assertEqual(result["text"], "Hi there")
        report = mc.report_markdown({"created_at": "now", "models": [
            {"provider": "gemini", "alias": "main", "model": "test",
             "protocol": "gemini-generate-content"}],
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
        self.assertEqual(mc.api_key_for({"api_key": "private"}), "private")


if __name__ == "__main__":
    unittest.main()
