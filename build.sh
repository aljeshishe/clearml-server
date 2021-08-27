#!/bin/bash
set -ex
docker build . -t aljeshishe/clearml:1.0.2-rc
docker push aljeshishe/clearml:1.0.2-rc
