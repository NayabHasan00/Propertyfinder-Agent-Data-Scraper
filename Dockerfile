FROM apify/actor-python-playwright:latest

# Copy requirements and install
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application
COPY . ./

# Run the main entrypoint
CMD ["python", "main.py"]