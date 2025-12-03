cd .....

python -m pip install -r requirements.txt
 
python manage.py makemigrations
python manage.py migrate
python manage.py runserver 0.0.0.0:8000

测试 http://localhost:8000