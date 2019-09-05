module.exports = {
    badRequest: function(res, errCode, value) {
        console.log(" error : " + errCode + " " + value);
        if(value) {
            res.statusText = JSON.stringify(value);
            return res.status(errCode).send();
        }
        res.statusText = JSON.stringify(errCode);
        return res.status(403).send();
    }
};