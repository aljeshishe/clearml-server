FROM harbor.srs.lab.nordigy.ru/dockerhub/allegroai/clearml:1.0.2
COPY apiserver /opt/clearml/apiserver
# for debug
#RUN yum install mc -y
#RUN pip3 install pydevd-pycharm==203.6682.179