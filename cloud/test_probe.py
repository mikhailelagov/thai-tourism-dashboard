import unittest
from probe import video_progressed


class ProgressTest(unittest.TestCase):
    def sample(self, current_time, **overrides):
        video = dict(width=640, height=360, readyState=4, currentTime=current_time,
                     paused=False, error=None)
        video.update(overrides)
        return {"videos": [video]}

    def test_decoding_requires_two_usable_frames(self):
        self.assertTrue(video_progressed([self.sample(5), self.sample(25)]))
        for second in (self.sample(5), self.sample(25, paused=True), self.sample(25, width=0),
                       self.sample(25, error=3), {"videos": []}):
            with self.subTest(second=second):
                self.assertFalse(video_progressed([self.sample(5), second]))
        self.assertFalse(video_progressed([]))


if __name__ == "__main__":
    unittest.main()
