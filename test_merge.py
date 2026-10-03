"""تست دستی: دانلود 720p با ادغام صدا/تصویر."""
import os
import time

import youtube_service as yt

if __name__ == '__main__':
    info = yt.fetch_info('https://www.youtube.com/watch?v=dQw4w9WgXcQ')
    t = time.time()
    path = yt.download(info['url'], '720', info['title'], lambda d: None)
    with open('t4.txt', 'w', encoding='utf-8') as f:
        f.write(f'OK path={path} size={os.path.getsize(path)} '
                f'secs={round(time.time() - t, 1)}')
