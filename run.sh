# 杀掉占用8000端口的进程
lsof -ti:8000 | xargs -r kill -9

python3 manage.py makemigrations
python3 manage.py migrate
python3 manage.py runserver 0.0.0.0:8000