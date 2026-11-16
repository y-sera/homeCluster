# nccl-tests (MPI 版) for DGX Spark (GB10 = sm_121, arm64), MPI Operator (MPIJob v2beta1) 用.
#
# ビルド (作業端末. buildx + qemu で arm64):
#   docker buildx build --platform linux/arm64 \
#     -t harbor.cluster.orenet.net/llm-system/nccl-tests:v2.21.1-cuda13.0.3 \
#     --push deploy/llm-system/images/nccl-tests
#
# - CUDA はホスト driver (580 = CUDA 13.0) に合わせて 13.0 系. NCCL はベースイメージ同梱の libnccl2 (2.28.3)
# - RDMA (RoCE) 用に rdma-core (libibverbs + mlx5 provider) を入れる. perftest は Pod 内の疎通確認用
# - sshd は MPI Operator 公式 base イメージに合わせて Port 2222, StrictModes no
#   (hostNetwork でもホストの sshd:22 と衝突しない). root で動かす
ARG CUDA_VERSION=13.0.3

FROM nvcr.io/nvidia/cuda:${CUDA_VERSION}-devel-ubuntu24.04 AS build
ARG NCCL_TESTS_VERSION=v2.21.1
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates make libopenmpi-dev openmpi-bin \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 -b ${NCCL_TESTS_VERSION} https://github.com/NVIDIA/nccl-tests.git /src/nccl-tests \
    && cd /src/nccl-tests \
    && make -j"$(nproc)" MPI=1 \
         MPI_HOME=/usr/lib/$(uname -m)-linux-gnu/openmpi \
         CUDA_HOME=/usr/local/cuda \
         NVCC_GENCODE="-gencode=arch=compute_121,code=[sm_121,compute_121]"

FROM nvcr.io/nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu24.04
ARG NCCL_TESTS_VERSION=v2.21.1
LABEL org.opencontainers.image.title="nccl-tests" \
      org.opencontainers.image.version="${NCCL_TESTS_VERSION}" \
      org.opencontainers.image.source="https://github.com/NVIDIA/nccl-tests"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         openmpi-bin openssh-server openssh-client \
         libibverbs1 ibverbs-providers librdmacm1 ibverbs-utils rdma-core perftest \
         iproute2 iputils-ping \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/nccl-tests/build/*_perf /usr/local/bin/

# MPI Operator が /root/.ssh に鍵 (Secret) をマウントする. known_hosts は書けないので無効化
RUN mkdir -p /var/run/sshd \
    && sed -i "s/[ #]\(.*StrictHostKeyChecking \).*/ \1no/g" /etc/ssh/ssh_config \
    && echo "    UserKnownHostsFile /dev/null" >> /etc/ssh/ssh_config \
    && sed -i "s/[ #]\(.*Port \).*/ \12222/g" /etc/ssh/ssh_config \
    && sed -i "s/#\(StrictModes \).*/\1no/g" /etc/ssh/sshd_config \
    && sed -i "s/#\(Port \).*/\12222/g" /etc/ssh/sshd_config

# root で mpirun を許可
ENV OMPI_ALLOW_RUN_AS_ROOT=1 \
    OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1

EXPOSE 2222
CMD ["/usr/sbin/sshd", "-De"]
