"""Execute production prepare/set methods in a host ownership fixture.

The fixture uses real POSIX descriptors but does not establish Android ABI
layout or HWC device execution. Set PYTHON to the intended interpreter and
TEST_OUTPUT_DIR to an existing job-scoped directory beneath the workbench's
trees/out directory, then run:
    "$PYTHON" graphics/composer/2.1/utils/hwc2on1adapter/tests/test_framebuffer_target.py
"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


def extract_method(source, signature):
    method_start = source.index(signature)
    method_end = source.index("\n}\n", method_start) + 3
    return source[method_start:method_end]


class FramebufferTargetTest(unittest.TestCase):
    def test_prepare_and_set_preserve_buffer_and_fence_ownership(self):
        output_directory = os.environ.get("TEST_OUTPUT_DIR")
        self.assertTrue(
            output_directory,
            "Set TEST_OUTPUT_DIR to an existing job-scoped directory under trees/out",
        )
        output_root = Path(output_directory)
        self.assertTrue(output_root.is_dir(), "TEST_OUTPUT_DIR must be a directory")
        adapter_source = Path(__file__).resolve().parents[1] / "HWC2On1Adapter.cpp"
        source = adapter_source.read_text()
        production_methods = "\n".join(
            extract_method(source, signature)
            for signature in (
                "void HWC2On1Adapter::Display::prepareFramebufferTarget() {",
                "Error HWC2On1Adapter::Display::set(hwc_display_contents_1& hwcContents) {",
            )
        )
        fixture = r"""
#include <cassert>
#include <cerrno>
#include <cinttypes>
#include <cstdint>
#include <memory>
#include <mutex>
#include <vector>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#define ALOGE(...) ((void)0)
#define ALOGV(...) ((void)0)
constexpr int HWC_FRAMEBUFFER = 0;
constexpr int HWC_FRAMEBUFFER_TARGET = 3;
constexpr int HWC_BLENDING_PREMULT = 1;
struct hwc_rect_t { int left, top, right, bottom; };
struct FloatRect { float left, top, right, bottom; };
struct Layer {
    const void* handle = nullptr;
    int compositionType = 0, releaseFenceFd = 0, hints = 0, flags = 0;
    int transform = 0, blending = 0, planeAlpha = 0, acquireFenceFd = 0;
    hwc_rect_t sourceCropi{}, displayFrame{};
    FloatRect sourceCropf{};
    struct { unsigned numRects = 0; hwc_rect_t* rects = nullptr; } visibleRegionScreen;
};
enum class Attribute { Width, Height };
enum class Error { None, NotValidated };
struct Config {
    int32_t getAttribute(Attribute attribute) const {
        return attribute == Attribute::Width ? 720 : 1280;
    }
};
struct Device {
    int version = 1;
    int getHwc1MinorVersion() const { return version; }
};
struct ClientTarget {
    const void* buffer = nullptr;
    int fence = -1;
    const void* getBuffer() const { return buffer; }
    int getFence() const { return ::dup(fence); }
};
struct hwc_display_contents_1 {
    size_t numHwLayers = 2;
    Layer hwLayers[2];
};
struct Changes {
    unsigned numTypes = 0;
    unsigned getNumTypes() const { return numTypes; }
};
hwc_rect_t* GetRects(unsigned count) {
    assert(count == 1);
    static hwc_rect_t visibleRect;
    return &visibleRect;
}
struct HWC2On1Adapter {
    struct Display {
        Config config;
        Config* mActiveConfig = &config;
        std::vector<int> mLayers = {0};
        hwc_display_contents_1 contents;
        hwc_display_contents_1* mHwc1RequestedContents = &contents;
        Device mDevice;
        ClientTarget mClientTarget;
        std::recursive_mutex mStateMutex;
        std::unique_ptr<Changes> mChanges;
        uint64_t mId = 1;
        void prepareFramebufferTarget();
        Error set(hwc_display_contents_1& hwcContents);
    };
};
"""
        checks = r"""
int main() {
    int firstBuffer = 1, nextBuffer = 2, unrelatedBuffer = 3;
    for (int hwcVersion : {1, 3}) {
        HWC2On1Adapter::Display display;
        display.mDevice.version = hwcVersion;
        auto& clientLayer = display.contents.hwLayers[0];
        auto& framebuffer = display.contents.hwLayers[1];
        clientLayer.handle = &unrelatedBuffer;
        display.prepareFramebufferTarget();
        assert(framebuffer.handle == nullptr);
        display.mClientTarget.buffer = &firstBuffer;
        framebuffer.acquireFenceFd = 31;
        framebuffer.releaseFenceFd = 32;
        display.prepareFramebufferTarget();
        assert(framebuffer.handle == &firstBuffer);
        assert(framebuffer.acquireFenceFd == -1);
        assert(framebuffer.releaseFenceFd == -1);
        assert(framebuffer.compositionType == HWC_FRAMEBUFFER_TARGET);
        assert(framebuffer.displayFrame.right == 720);
        assert(framebuffer.displayFrame.bottom == 1280);

        int sparePipe[2], layerPipe[2], targetPipe[2];
        assert(pipe(sparePipe) == 0);
        assert(pipe(layerPipe) == 0);
        assert(pipe(targetPipe) == 0);
        assert(sparePipe[0] < layerPipe[0]);
        clientLayer.compositionType = HWC_FRAMEBUFFER;
        clientLayer.acquireFenceFd = layerPipe[0];
        display.mClientTarget.buffer = &nextBuffer;
        display.mClientTarget.fence = targetPipe[0];

        assert(display.set(display.contents) == Error::NotValidated);
        assert(framebuffer.handle == &firstBuffer);
        assert(framebuffer.acquireFenceFd == -1);
        assert(clientLayer.acquireFenceFd == layerPipe[0]);
        assert(fcntl(layerPipe[0], F_GETFD) >= 0);
        assert(fcntl(targetPipe[0], F_GETFD) >= 0);
        display.mChanges = std::make_unique<Changes>();
        display.mChanges->numTypes = 1;
        assert(display.set(display.contents) == Error::NotValidated);
        assert(display.mChanges && display.mChanges->numTypes == 1);
        assert(framebuffer.handle == &firstBuffer);
        assert(framebuffer.acquireFenceFd == -1);
        assert(fcntl(layerPipe[0], F_GETFD) >= 0);
        assert(fcntl(targetPipe[0], F_GETFD) >= 0);

        // A lower free descriptor prevents dup from reusing the closed layer fd.
        assert(close(sparePipe[0]) == 0);
        display.mChanges->numTypes = 0;
        assert(display.set(display.contents) == Error::None);
        assert(!display.mChanges);
        assert(framebuffer.handle == &nextBuffer);
        assert(clientLayer.handle == &unrelatedBuffer);
        assert(clientLayer.acquireFenceFd == -1);
        errno = 0;
        assert(fcntl(layerPipe[0], F_GETFD) == -1);
        assert(errno == EBADF);
        const int presentedFence = framebuffer.acquireFenceFd;
        assert(presentedFence >= 0 && presentedFence != targetPipe[0]);
        assert(fcntl(presentedFence, F_GETFD) >= 0);
        assert(fcntl(targetPipe[0], F_GETFD) >= 0);
        struct stat originalStatus{}, duplicateStatus{};
        assert(fstat(targetPipe[0], &originalStatus) == 0);
        assert(fstat(presentedFence, &duplicateStatus) == 0);
        assert(S_ISFIFO(originalStatus.st_mode));
        assert(originalStatus.st_dev == duplicateStatus.st_dev);
        assert(originalStatus.st_ino == duplicateStatus.st_ino);
        assert(display.set(display.contents) == Error::NotValidated);
        assert(framebuffer.acquireFenceFd == presentedFence);
        assert(fcntl(presentedFence, F_GETFD) >= 0);
        assert(close(presentedFence) == 0);
        assert(fcntl(targetPipe[0], F_GETFD) >= 0);
        assert(close(targetPipe[0]) == 0);
        assert(close(targetPipe[1]) == 0);
        assert(close(layerPipe[1]) == 0);
        assert(close(sparePipe[1]) == 0);

        display.mClientTarget.fence = -1;
        display.prepareFramebufferTarget();
        assert(framebuffer.handle == &nextBuffer);
        assert(framebuffer.acquireFenceFd == -1);
        assert(framebuffer.releaseFenceFd == -1);
        display.mClientTarget.buffer = nullptr;
        display.prepareFramebufferTarget();
        assert(framebuffer.handle == nullptr);
    }
}
"""
        with tempfile.TemporaryDirectory(dir=output_root) as temporary:
            directory = Path(temporary)
            fixture_source = directory / "framebuffer_target.cpp"
            executable = directory / "framebuffer_target"
            fixture_source.write_text(fixture + production_methods + checks)
            subprocess.run(
                [os.environ.get("CXX", "c++"), "-std=c++17", "-Wall", "-Wextra",
                 "-Werror", str(fixture_source), "-o", str(executable)], check=True,
            )
            subprocess.run([str(executable)], check=True)


if __name__ == "__main__":
    unittest.main()
