# Use an official Python runtime as a parent image
FROM public.ecr.aws/docker/library/python:3.12-slim

# Set the working directory in the container to /app
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# Create required directories
RUN mkdir -p /output/AutotagPDF \
    /output/ExtractTextInfoFromPDF \
    /output/zipfile/images \
    /opt/verapdf

# veraPDF CLI (PDF/UA-1 gate before Adobe Autotag). Keep heap under Fargate 1GB.
ENV VERAPDF_VERSION=1.30
ENV VERAPDF_MINOR_VERSION=1
ENV JAVA_TOOL_OPTIONS="-Xmx384m"
ENV PATH="/opt/verapdf:${PATH}"

RUN apt-get update && \
    apt-get install -y --no-install-recommends default-jre-headless wget unzip ca-certificates && \
    wget -qO /tmp/verapdf-installer.zip \
      https://software.verapdf.org/releases/${VERAPDF_VERSION}/verapdf-greenfield-${VERAPDF_VERSION}.${VERAPDF_MINOR_VERSION}-installer.zip && \
    unzip -q /tmp/verapdf-installer.zip -d /tmp && \
    java -Djava.awt.headless=true -jar \
      /tmp/verapdf-greenfield-${VERAPDF_VERSION}.${VERAPDF_MINOR_VERSION}/verapdf-izpack-installer-${VERAPDF_VERSION}.${VERAPDF_MINOR_VERSION}.jar \
      /app/verapdf-install.xml && \
    ln -sf /opt/verapdf/verapdf /usr/local/bin/verapdf && \
    rm -rf /tmp/verapdf-installer.zip /tmp/verapdf-greenfield-${VERAPDF_VERSION}.${VERAPDF_MINOR_VERSION} && \
    apt-get purge -y wget unzip && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies and clean up unnecessary packages
RUN pip install --no-cache-dir -r requirements.txt && \
    # Remove documentation/sphinx packages pulled in by pdfservices-sdk (saves ~70MB)
    pip uninstall -y sphinx sphinx-rtd-theme sphinxcontrib-applehelp \
        sphinxcontrib-devhelp sphinxcontrib-htmlhelp sphinxcontrib-jquery \
        sphinxcontrib-jsmath sphinxcontrib-qthelp sphinxcontrib-serializinghtml \
        babel pygments alabaster imagesize snowballstemmer docutils 2>/dev/null || true && \
    # Remove pip and setuptools (not needed at runtime, saves ~18MB)
    pip uninstall -y pip setuptools 2>/dev/null || true && \
    # Clean up cache
    rm -rf /root/.cache /tmp/*

# Set the command to run the Python script when the container launches
CMD ["python", "./adobe_autotag_processor.py"]
