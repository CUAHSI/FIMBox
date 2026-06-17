FROM mambaorg/micromamba:debian12-slim

WORKDIR /home
USER root

# Install System dependencies
RUN apt update \
 && apt install -y \
 curl \
 libhdf5-dev \
 libnetcdf-dev \
 git \
 awscli \
 gdal-bin \
 libgdal-dev \
 && rm -rf /var/lib/apt/lists/*

# Set GDAL version env so pip installs correct Python bindings
ENV CPLUS_INCLUDE_PATH=/usr/include/gdal
ENV C_INCLUDE_PATH=/usr/include/gdal
ENV GDAL_VERSION=3.7.0

USER $MAMBA_USER

# Install Python dependencies
ARG MAMBA_DOCKERFILE_ACTIVATE=1
RUN micromamba install -y \
  python==3.10 \
  pip \
  typer \
  uv \
&& micromamba clean --all --yes


# Perform initial clone of flood_inundation code so this doesn't need to be done everytime.
# Something related to Numba and Geocube changed after this SHA was created 
# which caused the mosaicing process to fail.
ENV SHA=9d39cb85ae789b30964a82c19dd850d29fccf5df
RUN git clone --depth 1 https://github.com/NOAA-OWP/inundation-mapping.git /home/code/inundation-mapping \
 && cd /home/code/inundation-mapping \
 && git fetch origin ${SHA} \
 && git checkout ${SHA}


# Install FIMServ, patch the FIMServ library to allow 
# running flood_inundation calculations concurrently
# without merging the wrong output files.
ARG BRANCH="mods-for-docker-execution"
ARG FIM_REPO="https://github.com/CUAHSI/FIMserv.git"
RUN git clone -b $BRANCH $FIM_REPO /home/code/FIMServ

# patch FIMServ to remove unnecessary code
COPY patches/fimserve__init__.py /home/code/FIMServ/src/fimserve/__init__.py
COPY patches/fimserve_pyproject.toml /home/code/FIMServ/pyproject.toml

RUN cd /home/code/FIMServ \
 && uv pip install --system -e . 

##RUN git clone https://github.com/sdmlua/FIMserv.git /home/code/FIMServ 
### && /opt/conda/bin/pip install -e . 
##COPY runFIM.py /home/code/FIMServ/src/fimserve/runFIM.py



COPY compute_rating_increments.py /home/compute_rating_increments.py
COPY entry.py /home/entry.py
RUN mkdir -p /home/data/inputs

# Set bash as the default command
ENTRYPOINT ["/opt/conda/bin/python", "entry.py"]
CMD ["--help"]




