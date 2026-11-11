#include "cli/realtime_options.hpp"
#include "core/runtime/http_server.hpp"
#include <cstdlib>
#include <iostream>
#include <map>

static void check(bool condition, const char * message) {
    if (!condition) { std::cerr << "FAILED: " << message << '\n'; std::exit(1); }
}

int main() {
    using namespace vqa::rt;
    std::map<std::string, std::string> input;
    const auto get = [&](const char * key) { return input[key]; };
    Options o; std::string error;
    check(parse_options(get, o, error), "defaults must be usable");
    check(o.frames == 1 && o.observe_tokens < o.answer_tokens, "bounded realtime defaults");
    check(!o.recover_repeats, "history recovery must be opt-in");
    input = {{"recover-repeats", "1"}};
    check(parse_options(get, o, error) && o.recover_repeats, "repeated-observation recovery opt-in");
    input = {{"recover-repeats", "2"}}; error.clear();
    check(!parse_options(get, o, error), "invalid recovery switches must be rejected");
    input = {{"recover-repeats", "0"}}; error.clear();
    check(parse_options(get, o, error) && !o.recover_repeats, "recovery can preserve full history");
    input = {{"interval", "1.5"}, {"video-size", "1920x1080"}, {"capture-fps", "60"},
             {"n", "1024"}, {"observe-n", "256"}, {"warmup", "2.5"}, {"max-pixels", "524288"}};
    check(parse_options(get, o, error) && o.answer_tokens == 1024 && o.capture_fps == 60 && o.warmup == 2.5,
          "capture and generation settings must be independent");
    for (const auto & bad : {"nan", "inf", "-1", "1.5x", "999999999999999999999"}) {
        input = {{"interval", bad}}; error.clear();
        check(!parse_options(get, o, error), "invalid intervals must be rejected");
    }
    input = {{"n", "100.5"}}; error.clear();
    check(!parse_options(get, o, error), "integer budgets must not truncate decimals");
    input = {{"video-size", "1920x1080 -i another"}}; error.clear();
    check(!parse_options(get, o, error), "capture size is a resolution, not command text");
    input = {{"pixel-format", "yuyv422\" -i other"}}; error.clear();
    check(!parse_options(get, o, error), "pixel-format must be a single codec argument");
    input = {{"video-size", "auto"}, {"sys", "0"}}; error.clear();
    check(parse_options(get, o, error) && o.video_size.empty() && !o.system, "runtime fallback modes");

    check(vqa::http::query_param("session=rt123&n=512", "n") == "512", "n must not match the end of session");
    check(vqa::http::query_param("session=rt123", "n").empty(), "absent n must stay absent");
    check(vqa::http::query_param("q=%E4%BD%A0%E5%A5%BD&src=video%3DDroidCam+Video", "src") == "video=DroidCam Video",
          "encoded camera names");
    Utf8Output utf8;
    check(utf8.append("A\xE4").compare("A") == 0, "only complete UTF-8 is emitted");
    check(utf8.append("\xBD").empty(), "partial Chinese character is carried");
    check(utf8.append("\xA0\xE5\xA5\xBD") == "你好", "split Chinese tokens must reassemble exactly");
    check(utf8.append("\xF0\x9F").empty(), "four-byte character prefix is carried");
    check(utf8.append("\x8C\xBF") == "🌿", "four-byte character reassembly");
#ifdef _WIN32
    check(quote_arg("D:\\folder with spaces\\") == "\"D:\\folder with spaces\\\\\"", "Windows trailing slash quoting");
    check(quote_arg("video=Camera \"A\"") == "\"video=Camera \\\"A\\\"\"", "Windows quote escaping");
#endif
    std::cout << "Runtime options, query boundaries, UTF-8 streaming and capture quoting passed.\n";
}
