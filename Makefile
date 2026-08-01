.PHONY: proto test

# Перегенерация клиента Tinkoff из официальных контрактов.
#
# Контракты лежат в proto/tinkoff и взяты из RussianInvestments/investAPI.
# Готового SDK не используем: пакет tinkoff-investments снят с PyPI, а
# оставшийся под похожим именем не обновлялся с 2021 года.
#
# ВАЖНО. Версия grpcio-tools должна совпадать с grpcio/protobuf из
# requirements.txt: сгенерированный код требует runtime не ниже того, которым
# сгенерирован. Иначе приложение упадёт при импорте, а не при вызове.
#
# google/api не генерируем: его даёт googleapis-common-protos. Своя копия
# приводит к двойной регистрации одного файла в дескрипторном пуле.
PROTO_OUT = src/collector/tinkoff_pb

proto:
	pip install -q "grpcio-tools==1.80.0" "protobuf==6.33.6"
	rm -rf $(PROTO_OUT)
	mkdir -p $(PROTO_OUT)
	python3 -m grpc_tools.protoc -Iproto/tinkoff \
		--python_out=$(PROTO_OUT) --grpc_python_out=$(PROTO_OUT) \
		common.proto marketdata.proto google/api/field_behavior.proto
	rm -rf $(PROTO_OUT)/google
	sed -i 's/^import common_pb2 as common__pb2$$/from . import common_pb2 as common__pb2/' $(PROTO_OUT)/*.py
	sed -i 's/^import marketdata_pb2 as marketdata__pb2$$/from . import marketdata_pb2 as marketdata__pb2/' $(PROTO_OUT)/*.py
	git checkout $(PROTO_OUT)/__init__.py 2>/dev/null || true
	python3 -c "from src.collector.tinkoff_pb import marketdata_pb2_grpc; print('клиент собран')"

test:
	python3 -m pytest tests/ -q
