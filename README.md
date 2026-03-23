## 部署与一键更新

前端 / Qt 后端的构建、上传规则与 `quarcs-update` 用法见仓库根目录 **[README.md](../README.md)**（与 `../upload.py` 配套）。

---

## 本地运行 Django 上传服务

```bash
cd /path/to/QUARCS_upload   # 本仓库中的 django_upload 项目目录
python3 -m pip install -r requirements.txt
python3 manage.py makemigrations
python3 manage.py migrate
python3 manage.py runserver 0.0.0.0:8000
```

浏览器测试：<http://localhost:8000>