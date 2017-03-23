echo 'installing pip packages'
pip install -r req.txt
echo 'installing build_ext'
./setup.py install build_ext --inplace
