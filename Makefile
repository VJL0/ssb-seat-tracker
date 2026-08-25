.PHONY: build-TrackerFunction

build-TrackerFunction:
	uv export \
		--frozen \
		--no-dev \
		--no-editable \
		--no-emit-workspace \
		--no-header \
		--format requirements-txt \
		--output-file "$(ARTIFACTS_DIR)/requirements.txt"

	uv pip install \
		--no-installer-metadata \
		--no-compile-bytecode \
		--python-platform aarch64-manylinux2014 \
		--python 3.14 \
		--only-binary :all: \
		--target "$(ARTIFACTS_DIR)" \
		--requirements "$(ARTIFACTS_DIR)/requirements.txt"

	rm "$(ARTIFACTS_DIR)/requirements.txt"

	cp -R src/ssb_seat_tracker "$(ARTIFACTS_DIR)/"