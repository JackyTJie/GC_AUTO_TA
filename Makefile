PY ?= python3
CONFIG ?= config.yaml

.PHONY: help install check new-config doctor run run-live pull unpack provision grade report writeback status clean

help:
	@echo "make install     安装 Python 依赖"
	@echo "make check       语法检查（compileall）"
	@echo "make new-config  复制 config.example.yaml → config.yaml（权限 600）"
	@echo "make doctor      全链路自检（只读）"
	@echo "make run         全流程（回写 dry）"
	@echo "make run-live    全流程 + 真正回写 Canvas"
	@echo "make pull/unpack/provision/grade/report/writeback  分步执行"
	@echo "make status      查看状态库"
	@echo "make clean       清掉 work/ reports/（保留 state/）"

install:
	$(PY) -m pip install -r requirements.txt

check:
	$(PY) -m compileall -q gc_auto_ta && echo "语法检查通过"

new-config:
	$(PY) -m gc_auto_ta new-config -o $(CONFIG)

doctor:
	$(PY) -m gc_auto_ta doctor -c $(CONFIG)

run:
	$(PY) -m gc_auto_ta run -c $(CONFIG)

run-live:
	$(PY) -m gc_auto_ta run -c $(CONFIG) --live

pull:
	$(PY) -m gc_auto_ta pull -c $(CONFIG)

unpack:
	$(PY) -m gc_auto_ta unpack -c $(CONFIG)

provision:
	$(PY) -m gc_auto_ta provision -c $(CONFIG)

grade:
	$(PY) -m gc_auto_ta grade -c $(CONFIG)

report:
	$(PY) -m gc_auto_ta report -c $(CONFIG)

writeback:
	$(PY) -m gc_auto_ta writeback -c $(CONFIG)

writeback-live:
	$(PY) -m gc_auto_ta writeback -c $(CONFIG) --live

status:
	$(PY) -m gc_auto_ta status -c $(CONFIG)

clean:
	rm -rf work reports
