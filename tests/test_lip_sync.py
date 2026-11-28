import unittest

import httpx

from lip_sync import quote, upload_assets


class LipSyncTest(unittest.IsolatedAsyncioTestCase):
    def test_supported_lengths(self):
        self.assertEqual([quote(seconds)["recommended_credits"] for seconds in (6, 8, 10)], [84, 112, 140])
        with self.assertRaises(ValueError):
            quote(11)

    async def test_uploads_use_scoped_provider_urls_without_api_token(self):
        requests = []

        def handle(request):
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(200, json={"items": [
                    {"upload_url": "https://videos.magichour.ai/api-assets/a/video.mp4?sig=test",
                     "file_path": "api-assets/a/video.mp4"},
                    {"upload_url": "https://videos.magichour.ai/api-assets/b/audio.wav?sig=test",
                     "file_path": "api-assets/b/audio.wav"},
                ]})
            return httpx.Response(200)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            paths = await upload_assets(client, {"Authorization": "Bearer test"}, b"video", b"voice")
        self.assertEqual(paths, ["api-assets/a/video.mp4", "api-assets/b/audio.wav"])
        self.assertEqual(requests[0].headers["authorization"], "Bearer test")
        self.assertEqual([request.content for request in requests[1:]], [b"video", b"voice"])
        self.assertTrue(all("authorization" not in request.headers for request in requests[1:]))

    async def test_rejects_unexpected_upload_host(self):
        def handle(request):
            self.assertEqual(request.method, "POST")
            return httpx.Response(200, json={"items": [
                {"upload_url": "https://other.example/api-assets/a/video.mp4", "file_path": "api-assets/a/video.mp4"},
                {"upload_url": "https://videos.magichour.ai/api-assets/b/audio.wav", "file_path": "api-assets/b/audio.wav"},
            ]})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with self.assertRaises(ValueError):
                await upload_assets(client, {"Authorization": "Bearer test"}, b"video", b"voice")
