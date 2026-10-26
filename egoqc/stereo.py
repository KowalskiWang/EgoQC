"""Stereo geometry helpers shared by the checks (KB / OpenCV-fisheye camera model).

Rectified coordinates use a fixed focal length F = 500 px so residuals are comparable between devices.
"""
import cv2
import numpy as np

F = 500.0


class Rectifier:
    def __init__(self, c0, c1):
        self.c0, self.c1 = c0, c1
        w, h = c0["size"]
        self.size = (w, h)
        self.R1, self.R2, *_ = cv2.fisheye.stereoRectify(c0["K"], c0["D"], c1["K"], c1["D"], (w, h), c1["R"],
                                                         c1["t"].reshape(3, 1), flags=cv2.CALIB_ZERO_DISPARITY)
        self.P = np.array([[F, 0, w / 2], [0, F, h / 2], [0, 0, 1.0]])
        self.B = float(np.linalg.norm(c1["t"]))
        self._maps = None

    def maps(self):
        if self._maps is None:
            m0 = cv2.fisheye.initUndistortRectifyMap(self.c0["K"], self.c0["D"], self.R1, self.P, self.size, cv2.CV_32FC1)
            m1 = cv2.fisheye.initUndistortRectifyMap(self.c1["K"], self.c1["D"], self.R2, self.P, self.size, cv2.CV_32FC1)
            self._maps = (m0, m1)
        return self._maps

    def rect_points(self, uv, side):
        c, R = (self.c0, self.R1) if side == 0 else (self.c1, self.R2)
        return cv2.fisheye.undistortPoints(np.asarray(uv, np.float64).reshape(-1, 1, 2), c["K"], c["D"], R=R, P=self.P).reshape(-1, 2)

    def rectify(self, L, R):
        (m0, m1) = self.maps()
        if L.shape[1] != self.size[0] or L.shape[0] != self.size[1]:
            L = cv2.resize(L, self.size); R = cv2.resize(R, self.size)
        return cv2.remap(L, m0[0], m0[1], cv2.INTER_LINEAR), cv2.remap(R, m1[0], m1[1], cv2.INTER_LINEAR)

    def triangulate(self, rl, rr):
        """Rectified left/right points -> 3D in the left camera frame (units of the baseline)."""
        disp = rl[:, 0] - rr[:, 0]
        Z = F * self.B / np.where(disp > 0.5, disp, np.nan)
        w, h = self.size
        Xr = np.stack([(rl[:, 0] - w / 2) * Z / F, ((rl[:, 1] + rr[:, 1]) / 2 - h / 2) * Z / F, Z], 1)
        return Xr @ self.R1


_SIFT = None


def epipolar_residuals(rect, frames, max_features=6000):
    """frames: list of (L, R). Returns (dy array, dx array, (x,y) of left points) of mutual-NN SIFT matches."""
    global _SIFT
    if _SIFT is None:
        _SIFT = cv2.SIFT_create(nfeatures=max_features)
    bf = cv2.BFMatcher(cv2.NORM_L2)
    dys, dxs, xys, per = [], [], [], []
    for L, R in frames:
        Lr, Rr = rect.rectify(L, R)
        gl, gr = cv2.cvtColor(Lr, cv2.COLOR_BGR2GRAY), cv2.cvtColor(Rr, cv2.COLOR_BGR2GRAY)
        kl, dl = _SIFT.detectAndCompute(gl, None)
        kr, dr = _SIFT.detectAndCompute(gr, None)
        if dl is None or dr is None or len(kl) < 10 or len(kr) < 10:
            continue
        back = {m[0].queryIdx: m[0].trainIdx for m in bf.knnMatch(dr, dl, k=2) if len(m) == 2 and m[0].distance < 0.8 * m[1].distance}
        good = [m[0] for m in bf.knnMatch(dl, dr, k=2) if len(m) == 2 and m[0].distance < 0.75 * m[1].distance
                and back.get(m[0].trainIdx) == m[0].queryIdx]
        if len(good) < 10:
            continue
        pl = np.array([kl[m.queryIdx].pt for m in good]); pr = np.array([kr[m.trainIdx].pt for m in good])
        dy, dx = pl[:, 1] - pr[:, 1], pl[:, 0] - pr[:, 0]
        keep = (np.abs(dy) < 25) & (dx > 0.5)
        dys.append(dy[keep]); dxs.append(dx[keep]); xys.append(pl[keep]); per.append(float(np.median(dy[keep])) if keep.any() else np.nan)
    if not dys:
        return np.zeros(0), np.zeros(0), np.zeros((0, 2)), []
    return np.concatenate(dys), np.concatenate(dxs), np.concatenate(xys), per


def rectified_preview(rect, L, R, scale=0.5, crop=(0.2, 0.8)):
    Lr, Rr = rect.rectify(L, R)
    vis = np.hstack([Lr, Rr])
    for y in range(0, vis.shape[0], 40):
        cv2.line(vis, (0, y), (vis.shape[1], y), (0, 220, 0), 1)
    h = vis.shape[0]
    vis = vis[int(h * crop[0]):int(h * crop[1])]
    return cv2.resize(vis, None, fx=scale, fy=scale)


def kabsch(A, B):
    """R minimising sum |B - R A|^2 (rows are vectors)."""
    U, _, Vt = np.linalg.svd(A.T @ B)
    D = np.diag([1, 1, np.sign(np.linalg.det(Vt.T @ U.T))])
    return Vt.T @ D @ U.T


def bearings(pts, K, D):
    u = cv2.fisheye.undistortPoints(np.asarray(pts, np.float64).reshape(-1, 1, 2), K, D).reshape(-1, 2)
    b = np.c_[u, np.ones(len(u))]
    return b / np.linalg.norm(b, axis=1, keepdims=True)


def rotation_between(prev_gray, gray, K, D, top_frac=0.6):
    """Camera rotation between two frames from background features (rotation-only fit, robust reweighting).
    Returns rotation vector of the camera (camera frame, rad) or None."""
    h = gray.shape[0]
    mask = np.zeros_like(gray); mask[: int(h * top_frac)] = 255
    p0 = cv2.goodFeaturesToTrack(prev_gray, 300, 0.01, 12, mask=mask)
    if p0 is None or len(p0) < 30:
        return None
    p1, st, _ = cv2.calcOpticalFlowPyrLK(prev_gray, gray, p0, None, winSize=(21, 21), maxLevel=3)
    ok = st.ravel() == 1
    if ok.sum() < 30:
        return None
    A, B = bearings(p0[ok], K, D), bearings(p1[ok], K, D)
    w = np.ones(len(A))
    for _ in range(3):
        Rm = kabsch(A * w[:, None], B * w[:, None])
        res = np.degrees(np.arccos(np.clip(np.sum((A @ Rm.T) * B, 1), -1, 1)))
        w = (res < max(0.15, 3 * np.median(res))).astype(float)
    if w.mean() < 0.5:
        return None
    return -cv2.Rodrigues(Rm)[0].ravel()   # bearings rotate opposite to the camera
