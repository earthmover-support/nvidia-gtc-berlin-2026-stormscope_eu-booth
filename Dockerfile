# syntax=docker/dockerfile:1
# StormScope-EU pipeline (scripts/stormscope_eu_pipeline.py) on a CUDA GPU host.
# Start it with `docker compose up -d` (see compose.yaml), which also supplies the credentials.

FROM nvcr.io/nvidia/physicsnemo/physicsnemo:26.08

ARG EARTH2STUDIO_COMMIT=e916317a7e53ee73cc8cf5cd97064e970cfd723e

# The base image's torch, physicsnemo and numpy are CUDA builds; pin them so pip resolves
# the packages below around them instead of swapping in PyPI wheels.
RUN pip list --format=freeze \
    | grep -iE '^(torch|torchvision|torchaudio|triton|nvidia-physicsnemo|numpy)==' \
    > /opt/base-constraints.txt

# earth2studio without extras: [data] pulls cfgrib/eccodes and [stormscope] pulls
# earth2grid and NATTEN, none of which StormScopeMeteosatEU uses. What it does use from
# those extras (eumdac, icechunk, arraylake, pyproj, nvtx, omegaconf) is listed here.
RUN pip install --no-cache-dir -c /opt/base-constraints.txt \
    "earth2studio @ https://github.com/NVIDIA/earth2studio/archive/${EARTH2STUDIO_COMMIT}.tar.gz" \
    "eumdac>=3.1.1" \
    "icechunk>=2.2" \
    "arraylake>=1.3" \
    "pyproj>=3.7.1" \
    "nvtx>=0.2.11" \
    "omegaconf>=2.3.0"

RUN pip install natten==0.21.7+torch2130cu130 -f https://whl.natten.org

# Fail the build, not the first run, if a dependency is missing: StormScopeMeteosatEU
# only reports its optional imports when it is instantiated. The physicsnemo modules it
# needs are located rather than imported: importing them loads transformer_engine, which
# needs libcuda.so.1, and builds run without the GPU driver.
RUN python -c "\
import importlib.util, pathlib; \
import arraylake, earth2studio, eumdac, icechunk, nvtx, omegaconf, pyproj; \
root = pathlib.Path(importlib.util.find_spec('physicsnemo').submodule_search_locations[0]); \
needed = ['diffusion/noise_schedulers', 'diffusion/preconditioners', 'diffusion/samplers', \
          'nn/module/dit_layers', 'utils/zenith_angle']; \
missing = [m for m in needed if not (root / m).is_dir() and not (root / m).with_suffix('.py').is_file()]; \
assert not missing, f'physicsnemo is missing {missing}'"

# Model checkpoint and downloaded FCI products; compose.yaml mounts a volume here.
ENV EARTH2STUDIO_CACHE=/cache/earth2studio \
    HF_HOME=/cache/huggingface \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY scripts/ scripts/
COPY --chmod=0755 docker/entrypoint.sh /usr/local/bin/stormscope-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/stormscope-entrypoint.sh"]
CMD ["python", "scripts/stormscope_eu_pipeline.py"]
