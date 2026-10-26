"""Thresholds and defaults. Every threshold the report uses lives here so it can be read and tuned in one place."""
import os

OUTPUT_ROOT = os.environ.get("EGOQC_OUTPUT", os.path.join(os.path.expanduser("~"), "EgoQC_Reports"))
LANG = os.environ.get("EGOQC_LANG", "zh")       # default language of the report (it can be switched inside the report)

# analysis budget per recording (heavy checks decode at most this much video)
MAX_SECONDS = 150
HAND_MAX_PAIRS = 1500          # MediaPipe on at most this many stereo pairs (evenly strided)
SELFCALIB_FRAMES = 150         # COLMAP self-calibration uses at most this many distinct frames
SELFCALIB_THREADS = 6

PASS, WARN, FAIL, NA, INFO = "pass", "warn", "fail", "na", "info"
LABEL = {PASS: {"zh": "通过", "en": "Pass"}, WARN: {"zh": "注意", "en": "Warning"}, FAIL: {"zh": "不通过", "en": "Fail"},
         NA: {"zh": "无法检查", "en": "N/A"}, INFO: {"zh": "信息", "en": "Info"}}
TITLES = {"inventory": {"zh": "数据完整性", "en": "Completeness"},
          "frames": {"zh": "真实帧率与时间戳", "en": "Real frame rate & timestamps"},
          "sync": {"zh": "左右同步", "en": "Left/right sync"},
          "geometry": {"zh": "双目标定与几何", "en": "Stereo calibration"},
          "hands": {"zh": "手部两目对应", "en": "Hands in both views"},
          "scale": {"zh": "空间尺度", "en": "Metric scale"},
          "imu": {"zh": "相机-IMU", "en": "Camera–IMU"},
          "quality": {"zh": "码率与压缩", "en": "Bitrate & compression"}}

T = dict(
    real_fps_pass=0.95, real_fps_warn=0.80,      # distinct frames / nominal fps
    gap_warn_ms=100, gap_fail_ms=250,             # longest interval between real frames
    repeat_mad=1.0,                               # mean |grey diff| below which a frame repeats the previous one
    lr_ts_pass_us=1000,                           # mean |t_left - t_right|
    epi_pass_px=0.8, epi_warn_px=1.5,             # median |dy| after rectification (f = 500 px)
    epi_bias_px=0.4,                              # |median dy| above this = systematic misalignment
    det_pass=0.90, det_warn=0.75,                 # single-view hand detection rate
    clean_pass=0.60, clean_warn=0.40,             # stereo-consistent hand pairs
    palm_lo_m=0.070, palm_hi_m=0.100,             # wrist->middle MCP, adult range
    palm_bad_lo_m=0.060, palm_bad_hi_m=0.115,     # outside this the metric scale (baseline) is wrong
    palm_trend_warn=0.10, palm_trend_fail=0.20,   # palm length change per 10 cm wrist depth (both hands same sign)
    palm_span_min_m=0.15,                         # depth range (p5-p95) needed before the trend is judged
    jump_speed=2.4,                               # m/s, wrist speed above which a frame-to-frame step is a jump
    imu_rate_min=100, imu_gap_warn_ms=20, imu_gap_fail_ms=100, imu_freeze_s=0.2,
    imu_offset_pass_ms=10, imu_offset_warn_ms=33, imu_offset_spread_ms=50,
    bpp_h264_pass=0.08, bpp_h264_warn=0.04,       # bits per pixel per real frame per eye
    bpp_mjpeg_pass=0.30, bpp_mjpeg_warn=0.15,
)
