import cv2
import numpy as np
import os
import glob
import sys
import argparse
import time  # <--- 【修复 1】添加了这行导入

# ================= 配置区 =================
# 窗口名字定义为常量，防止改漏！
WINDOW_NAME = "HSV Tuner v3 Beta" 
DEFAULT_FOLDER = 'test_images'
SCALE_RATIO = 0.8
EXTENSIONS = ['*.png', '*.jpg', '*.jpeg', '*.bmp']
# =========================================

def safe_exit(code=0):
    if sys.platform == 'win32':
        print("\n" + "-"*30)
        input("按 [Enter] 键退出程序...")
    sys.exit(code)

def nothing(x):
    pass

def print_params(l_h, l_s, l_v, h_h, h_s, h_v):
    """格式化打印参数，方便复制"""
    print("\n" + "="*40)
    print(f"📋 [参数已捕获] - 请复制以下内容到 pipeline.json:")
    print("-" * 40)
    print(f'            "lower_hsv": [{l_h}, {l_s}, {l_v}],')
    print(f'            "upper_hsv": [{h_h}, {h_s}, {h_v}]')
    print("="*40)

def main():
    # --- 1. 参数与文件夹检查 ---
    parser = argparse.ArgumentParser(description="MAA HSV 调试工具")
    parser.add_argument("folder", nargs="?", default=DEFAULT_FOLDER)
    args = parser.parse_args()
    image_folder = args.folder

    if not os.path.exists(image_folder):
        print(f"❌ 文件夹不存在: '{image_folder}'")
        try:
            os.makedirs(image_folder)
            print(f"✨ 已自动创建: '{image_folder}' (请放入图片后重试)")
        except: pass
        safe_exit(1)

    image_paths = []
    for ext in EXTENSIONS:
        image_paths.extend(glob.glob(os.path.join(image_folder, ext)))

    if not image_paths:
        print(f"⚠️ 文件夹 '{image_folder}' 是空的！")
        safe_exit(1)

    # --- 2. 初始化窗口 ---
    total_imgs = len(image_paths)
    print(f" 成功加载 {total_imgs} 张图片")
    print("-" * 30)
    print("🎮 [操作指南]")
    print("   [ S ] : 🔥 保存参数到命令行 (推荐)")
    print("   [A/D] : 切换图片")
    print("   [Esc] : 退出")
    print("-" * 30)
    
    cv2.namedWindow(WINDOW_NAME, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WINDOW_NAME, 1280, 720)

    # 创建滑块
    cv2.createTrackbar('Low H', WINDOW_NAME, 0, 180, nothing)
    cv2.createTrackbar('Low S', WINDOW_NAME, 0, 255, nothing)
    cv2.createTrackbar('Low V', WINDOW_NAME, 120, 255, nothing)
    cv2.createTrackbar('High H', WINDOW_NAME, 180, 180, nothing)
    cv2.createTrackbar('High S', WINDOW_NAME, 60, 255, nothing)
    cv2.createTrackbar('High V', WINDOW_NAME, 255, 255, nothing)

    current_idx = 0
    need_refresh = True
    cached_img = None
    cached_hsv = None

    # 初始化变量
    l_h, l_s, l_v = 0, 0, 120
    h_h, h_s, h_v = 180, 60, 255
    
    # 【修复 2】初始化计时器变量，防止未按S前报错（虽非必须，但为了逻辑严谨）
    save_feedback_timer = 0 

    while True:
        # 检测窗口是否被用户点击 "X" 关闭了
        if cv2.getWindowProperty(WINDOW_NAME, cv2.WND_PROP_VISIBLE) < 1:
            print("\n🛑 窗口已关闭。")
            break

        # --- 图片加载逻辑 ---
        if need_refresh:
            img_path = image_paths[current_idx]
            original_img = cv2.imread(img_path)
            if original_img is None:
                current_idx = (current_idx + 1) % total_imgs
                continue
            
            if SCALE_RATIO != 1.0:
                h, w = original_img.shape[:2]
                cached_img = cv2.resize(original_img, (int(w*SCALE_RATIO), int(h*SCALE_RATIO)))
            else:
                cached_img = original_img
            
            cached_hsv = cv2.cvtColor(cached_img, cv2.COLOR_BGR2HSV)
            file_name = os.path.basename(img_path)
            need_refresh = False

        # --- 读取滑块 ---
        try:
            l_h = cv2.getTrackbarPos('Low H', WINDOW_NAME)
            l_s = cv2.getTrackbarPos('Low S', WINDOW_NAME)
            l_v = cv2.getTrackbarPos('Low V', WINDOW_NAME)
            h_h = cv2.getTrackbarPos('High H', WINDOW_NAME)
            h_s = cv2.getTrackbarPos('High S', WINDOW_NAME)
            h_v = cv2.getTrackbarPos('High V', WINDOW_NAME)
        except:
            break

        # --- 处理与显示 ---
        lower = np.array([l_h, l_s, l_v])
        upper = np.array([h_h, h_s, h_v])
        mask = cv2.inRange(cached_hsv, lower, upper)
        
        result = np.full_like(cached_img, 255)
        result[mask > 0] = [0, 0, 0]

        # UI 信息
        cv2.putText(result, f"[{current_idx+1}/{total_imgs}] {file_name}", (10, 30), 
                   cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
        
        # 【优化】如果刚按过 S 键（1.5秒内），在屏幕显示绿色提示
        if time.time() - save_feedback_timer < 1.5:
             cv2.putText(result, "SAVED!", (10, 70), 
                   cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)

        combined = np.hstack((cached_img, result))
        cv2.imshow(WINDOW_NAME, combined)

        # --- 按键 ---
        key = cv2.waitKey(20) & 0xFF
        
        if key == 27 or key == ord('q'): # Esc 或 Q
            break
            
        elif key == ord('s') or key == ord('p'): # [S] 保存
            print_params(l_h, l_s, l_v, h_h, h_s, h_v)
            save_feedback_timer = time.time() # 这里不再会报错了
            
        elif key == ord('a') or key == 81: # ←
            current_idx = (current_idx - 1 + total_imgs) % total_imgs
            need_refresh = True
            
        elif key == ord('d') or key == 83: # →
            current_idx = (current_idx + 1) % total_imgs
            need_refresh = True

    # 退出后打印最后一次的参数
    cv2.destroyAllWindows()
    print("\n" + "="*40)
    print("📋 [调试参数] (可以直接复制进 pipeline.json):")
    print(f'        "lower_hsv": [{l_h}, {l_s}, {l_v}],')
    print(f'        "upper_hsv": [{h_h}, {h_s}, {h_v}]')
    print("="*40)
    safe_exit(0)

if __name__ == "__main__":
    main()