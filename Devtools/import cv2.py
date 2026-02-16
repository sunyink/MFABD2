import cv2
import numpy as np
import os
import glob

# ================= 配置区 =================
# 把所有需要测试的截图都放在这个文件夹里！
IMAGE_FOLDER = 'test_images' 
# 支持的格式
EXTENSIONS = ['*.png', '*.jpg', '*.jpeg', '*.bmp']
# 缩放比例 (0.5 = 缩小一半，方便看全图)
SCALE_RATIO = 0.8 
# =========================================

def nothing(x):
    pass

# 1. 读取文件夹内所有图片
image_paths = []
for ext in EXTENSIONS:
    image_paths.extend(glob.glob(os.path.join(IMAGE_FOLDER, ext)))

if not image_paths:
    print(f"❌ 错误：在 '{IMAGE_FOLDER}' 文件夹里没找到图片！")
    print("请新建一个文件夹，把所有截图都丢进去。")
    exit()

print(f"📂 加载了 {len(image_paths)} 张图片")
current_idx = 0

# 初始化窗口
cv2.namedWindow('Multi-Image HSV Tuner', cv2.WINDOW_NORMAL)
cv2.resizeWindow('Multi-Image HSV Tuner', 1280, 720)

# 创建滑块 (默认值设为一组比较宽容的参数)
cv2.createTrackbar('Low H', 'Multi-Image HSV Tuner', 0, 180, nothing)
cv2.createTrackbar('Low S', 'Multi-Image HSV Tuner', 0, 255, nothing)
cv2.createTrackbar('Low V', 'Multi-Image HSV Tuner', 80, 255, nothing) # 稍微降低亮度下限以兼容灰色图标

cv2.createTrackbar('High H', 'Multi-Image HSV Tuner', 180, 180, nothing)
cv2.createTrackbar('High S', 'Multi-Image HSV Tuner', 60, 255, nothing)
cv2.createTrackbar('High V', 'Multi-Image HSV Tuner', 255, 255, nothing)

print("\n" + "="*50)
print("🎮 操作指南：")
print("   [ A / ← ] : 上一张图片")
print("   [ D / → ] : 下一张图片")
print("   [ Q / Esc]: 退出并输出参数")
print("🎯 目标：调整滑块，让【所有图片】里的图标都能显示出清晰的黑色轮廓")
print("="*50)

while True:
    # 读取当前图片
    img_path = image_paths[current_idx]
    img = cv2.imread(img_path)
    if img is None:
        print(f"无法读取: {img_path}")
        current_idx = (current_idx + 1) % len(image_paths)
        continue

    # 缩放
    if SCALE_RATIO != 1.0:
        img = cv2.resize(img, None, fx=SCALE_RATIO, fy=SCALE_RATIO)

    # 获取滑块值
    l_h = cv2.getTrackbarPos('Low H', 'Multi-Image HSV Tuner')
    l_s = cv2.getTrackbarPos('Low S', 'Multi-Image HSV Tuner')
    l_v = cv2.getTrackbarPos('Low V', 'Multi-Image HSV Tuner')
    h_h = cv2.getTrackbarPos('High H', 'Multi-Image HSV Tuner')
    h_s = cv2.getTrackbarPos('High S', 'Multi-Image HSV Tuner')
    h_v = cv2.getTrackbarPos('High V', 'Multi-Image HSV Tuner')

    # HSV 处理
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    lower = np.array([l_h, l_s, l_v])
    upper = np.array([h_h, h_s, h_v])
    
    mask = cv2.inRange(hsv, lower, upper)

    # 模拟 MAA 效果：白底黑图
    result = np.full_like(img, 255)
    result[mask > 0] = [0, 0, 0]

    # 在图像上显示当前文件名和索引
    cv2.putText(result, f"[{current_idx+1}/{len(image_paths)}] {os.path.basename(img_path)}", 
                (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)

    # 拼接显示 (左边原图，右边处理后)
    combined = np.hstack((img, result))
    cv2.imshow('Multi-Image HSV Tuner', combined)

    # 按键监听
    key = cv2.waitKey(30) & 0xFF
    
    if key == ord('q') or key == 27: # Q 或 Esc
        print("\n✅ 最终通用参数:")
        print(f'"lower_hsv": [{l_h}, {l_s}, {l_v}],')
        print(f'"upper_hsv": [{h_h}, {h_s}, {h_v}]')
        break
    elif key == ord('a') or key == 81: # A 或 左箭头
        current_idx = (current_idx - 1) % len(image_paths)
    elif key == ord('d') or key == 83: # D 或 右箭头
        current_idx = (current_idx + 1) % len(image_paths)

cv2.destroyAllWindows()