import cv2
import numpy as np
import os
import glob
import sys
import argparse
import time

# ================= 默认配置 =================
DEFAULT_FOLDER = 'test_images'
SCALE_RATIO = 0.8  # 视图缩放比例
EXTENSIONS = ['*.png', '*.jpg', '*.jpeg', '*.bmp']
# ===========================================

def safe_exit(code=0):
    """防止双击运行时窗口瞬间关闭，让用户能看清报错"""
    if sys.platform == 'win32':
        print("\n" + "-"*30)
        input("按 [Enter] 键退出程序...")
    sys.exit(code)

def nothing(x):
    pass

def main():
    # 1. 命令行参数解析
    parser = argparse.ArgumentParser(description="MAA HSV 阈值调试工具")
    parser.add_argument("folder", nargs="?", default=DEFAULT_FOLDER, help="图片文件夹路径")
    args = parser.parse_args()

    image_folder = args.folder

    # 2. 文件夹检查与自动创建
    if not os.path.exists(image_folder):
        print(f"❌ [错误] 文件夹不存在: '{image_folder}'")
        try:
            os.makedirs(image_folder)
            print(f"✨ [自动修复] 已为您创建文件夹: '{image_folder}'")
            print(f"👉 请将需要测试的截图放入该文件夹，然后重新运行脚本。")
        except Exception as e:
            print(f"❌ [致命] 无法创建文件夹: {e}")
        safe_exit(1)

    # 3. 读取图片
    image_paths = []
    print(f"🔍 正在扫描文件夹: {os.path.abspath(image_folder)}")
    
    for ext in EXTENSIONS:
        # 兼容 Windows/Linux 路径拼接
        search_pattern = os.path.join(image_folder, ext)
        found = glob.glob(search_pattern)
        image_paths.extend(found)

    if not image_paths:
        print(f"⚠️ [警告] 在 '{image_folder}' 中未找到图片！")
        print(f"ℹ️ 支持的格式: {', '.join(EXTENSIONS)}")
        safe_exit(1)

    # 4. 初始化
    total_imgs = len(image_paths)
    print(f"✅ 成功加载 {total_imgs} 张图片")
    print("-" * 40)
    print("🎮 [操作指南]")
    print("   [ A / ← ] : 上一张")
    print("   [ D / → ] : 下一张")
    print("   [ Q / Esc]: 保存参数并退出")
    print("-" * 40)

    cv2.namedWindow('HSV Tuner v3', cv2.WINDOW_NORMAL)
    cv2.resizeWindow('HSV Tuner v3', 1280, 720)

    # 创建滑块 (默认值优化：适应半透明物体的初始值)
    cv2.createTrackbar('Low H', 'HSV Tuner v3', 0, 180, nothing)
    cv2.createTrackbar('Low S', 'HSV Tuner v3', 0, 255, nothing)
    cv2.createTrackbar('Low V', 'HSV Tuner v3', 120, 255, nothing) # 默认 V=120

    cv2.createTrackbar('High H', 'HSV Tuner v3', 180, 180, nothing)
    cv2.createTrackbar('High S', 'HSV Tuner v3', 60, 255, nothing) # 默认 S=60
    cv2.createTrackbar('High V', 'HSV Tuner v3', 255, 255, nothing)

    current_idx = 0
    need_refresh_image = True
    cached_img = None
    cached_hsv = None

    while True:
        # --- 性能优化：只有切换图片时才读取和缩放 ---
        if need_refresh_image:
            img_path = image_paths[current_idx]
            original_img = cv2.imread(img_path)
            
            if original_img is None:
                print(f"❌ [读取失败] 文件可能已损坏: {img_path}")
                # 自动跳到下一张
                current_idx = (current_idx + 1) % total_imgs
                continue

            # 缩放处理
            if SCALE_RATIO != 1.0:
                h, w = original_img.shape[:2]
                new_size = (int(w * SCALE_RATIO), int(h * SCALE_RATIO))
                cached_img = cv2.resize(original_img, new_size)
            else:
                cached_img = original_img

            # 预转换 HSV (避免每帧重复计算)
            cached_hsv = cv2.cvtColor(cached_img, cv2.COLOR_BGR2HSV)
            
            # 提取文件名
            file_name = os.path.basename(img_path)
            need_refresh_image = False

        # --- 实时处理 ---
        # 获取滑块值
        l_h = cv2.getTrackbarPos('Low H', 'HSV Tuner v3')
        l_s = cv2.getTrackbarPos('Low S', 'HSV Tuner v3')
        l_v = cv2.getTrackbarPos('Low V', 'HSV Tuner v3')
        h_h = cv2.getTrackbarPos('High H', 'HSV Tuner v3')
        h_s = cv2.getTrackbarPos('High S', 'HSV Tuner v3')
        h_v = cv2.getTrackbarPos('High V', 'HSV Tuner v3')

        lower = np.array([l_h, l_s, l_v])
        upper = np.array([h_h, h_s, h_v])

        # 生成 Mask
        mask = cv2.inRange(cached_hsv, lower, upper)

        # 模拟效果：白底黑图
        result = np.full_like(cached_img, 255)
        result[mask > 0] = [0, 0, 0]

        # UI 文字信息
        info_text = f"[{current_idx+1}/{total_imgs}] {file_name}"
        cv2.putText(result, info_text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 
                   0.7, (0, 0, 255), 2, cv2.LINE_AA)
        
        param_text = f"L:[{l_h},{l_s},{l_v}] H:[{h_h},{h_s},{h_v}]"
        cv2.putText(result, param_text, (10, 70), cv2.FONT_HERSHEY_SIMPLEX, 
                   0.6, (255, 0, 0), 2, cv2.LINE_AA)

        # 拼接显示
        combined = np.hstack((cached_img, result))
        cv2.imshow('HSV Tuner v3', combined)

        # --- 按键监听 ---
        key = cv2.waitKey(20) & 0xFF

        if key == 27 or key == ord('q'): # Esc 或 q
            print("\n" + "="*40)
            print("📋 [调试完成] 请复制以下 JSON 到 pipeline.json:")
            print("-" * 40)
            print(f'        "lower_hsv": [{l_h}, {l_s}, {l_v}],')
            print(f'        "upper_hsv": [{h_h}, {h_s}, {h_v}]')
            print("-" * 40)
            break
        
        elif key == ord('a') or key == 81: # ←
            current_idx = (current_idx - 1 + total_imgs) % total_imgs
            need_refresh_image = True
            
        elif key == ord('d') or key == 83: # →
            current_idx = (current_idx + 1) % total_imgs
            need_refresh_image = True

    cv2.destroyAllWindows()
    safe_exit(0)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 用户强制中断")
        safe_exit(0)
    except Exception as e:
        print(f"\n❌ [程序崩溃] 未知错误: {e}")
        safe_exit(1)